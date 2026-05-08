"""
Benchmark USB capture service frame latency.

Measures the same localhost request path used by Alas:
service frame request -> color calibration -> metadata -> image transfer.
"""

import argparse
import os
import socket
import statistics
import sys
import time
from multiprocessing import shared_memory

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dev_tools.usb_capture_service import (
    FRAME_TIMEOUT,
    HOST,
    recv_json,
    send_json,
    service_port,
)


def parse_args():
    parser = argparse.ArgumentParser(description='Benchmark USB capture service latency.')
    parser.add_argument('config_name', nargs='?', default='alas', help='Alas config name, e.g. "alas (1)"')
    parser.add_argument('--count', type=int, default=50, help='Measured requests. Default: 50')
    parser.add_argument('--warmup', type=int, default=5, help='Warmup requests. Default: 5')
    parser.add_argument('--interval', type=float, default=0.3, help='Interval between requests in seconds. Default: 0.3')
    parser.add_argument('--raw', action='store_true', help='Request raw uncalibrated frames instead of calibrated frames')
    parser.add_argument('--socket', action='store_true', help='Use socket image transfer instead of shared memory')
    parser.add_argument('--no-persistent', action='store_true', help='Open a new TCP connection for every request')
    parser.add_argument('--timeout', type=float, default=FRAME_TIMEOUT, help='Socket timeout in seconds')
    return parser.parse_args()


def percentile(values, ratio):
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(len(values) * ratio) - 1))
    return sorted(values)[index]


def summarize(name, values):
    print(f'{name}:')
    print(f'  avg {statistics.mean(values):7.2f} ms')
    print(f'  p50 {statistics.median(values):7.2f} ms')
    print(f'  p90 {percentile(values, 0.90):7.2f} ms')
    print(f'  p99 {percentile(values, 0.99):7.2f} ms')
    print(f'  min {min(values):7.2f} ms')
    print(f'  max {max(values):7.2f} ms')


class BenchmarkConnection:
    def __init__(self, config_name, timeout=FRAME_TIMEOUT, persistent=True):
        self.config_name = config_name
        self.timeout = timeout
        self.persistent = persistent
        self.sock = None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def socket(self):
        if not self.persistent or self.sock is None:
            if self.sock is not None:
                self.close()
            start = time.perf_counter()
            self.sock = socket.create_connection((HOST, service_port(self.config_name)), timeout=self.timeout)
            self.sock.settimeout(self.timeout)
            return self.sock, (time.perf_counter() - start) * 1000
        self.sock.settimeout(self.timeout)
        return self.sock, 0.0


def request_frame(connection, raw=False, timeout=FRAME_TIMEOUT, use_shared_memory=True):
    if use_shared_memory:
        cmd = 'frame_shm_raw' if raw else 'frame_shm'
    else:
        cmd = 'frame_raw' if raw else 'frame'
    start_perf = time.perf_counter()
    sock, connect_ms = connection.socket()
    try:
        send_json(sock, {'cmd': cmd, 'profile': True})
        sent_perf = time.perf_counter()
        response = recv_json(sock, timeout=timeout)
        meta_perf = time.perf_counter()
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'USB capture service returned no frame'))

        size = int(response['size'])
        copy_start = time.perf_counter()
        if use_shared_memory:
            shm = shared_memory.SharedMemory(name=response['shm_name'], track=False)
            shm_opened = time.perf_counter()
            try:
                # Copy the frame just like Alas does, but do not keep it around.
                view = shm.buf[:size]
                bytes(view)
                del view
                copied_perf = time.perf_counter()
            finally:
                shm.close()
            transfer_ms = (copied_perf - copy_start) * 1000
            shm_open_ms = (shm_opened - copy_start) * 1000
            shm_copy_ms = (copied_perf - shm_opened) * 1000
        else:
            received = 0
            while received < size:
                chunk = sock.recv(size - received)
                if not chunk:
                    raise ConnectionError('Frame stream closed')
                received += len(chunk)
            copied_perf = time.perf_counter()
            transfer_ms = (copied_perf - copy_start) * 1000
            shm_open_ms = 0.0
            shm_copy_ms = 0.0
    except Exception:
        connection.close()
        raise
    finally:
        if not connection.persistent:
            connection.close()

    end_perf = time.perf_counter()
    frame_time = float(response.get('time', 0.0))
    profile = response.get('profile') or {}
    return {
        'seq': int(response.get('seq', 0)),
        'connect_ms': connect_ms,
        'send_ms': (sent_perf - start_perf) * 1000 - connect_ms,
        'header_ms': (meta_perf - start_perf) * 1000,
        'transfer_ms': transfer_ms,
        'shm_open_ms': shm_open_ms,
        'shm_copy_ms': shm_copy_ms,
        'total_ms': (end_perf - start_perf) * 1000,
        'frame_age_ms': (time.time() - frame_time) * 1000 if frame_time else 0.0,
        'size': size,
        'profile': profile,
        'server_frame_age_at_select_ms': float(profile.get('frame_age_at_select_ms', 0.0)),
        'server_select_ms': float(profile.get('select_ms', 0.0)),
        'server_normalize_ms': float(profile.get('normalize_ms', 0.0)),
        'server_color_correct_ms': float(profile.get('color_correct_ms', 0.0)),
        'server_shm_write_ms': float(profile.get('shm_write_ms', 0.0)),
        'server_handler_ms': float(profile.get('handler_before_reply_ms', 0.0)),
        'server_cache_hit': bool(profile.get('cache_hit', False)),
    }


def main():
    args = parse_args()
    print(f'Config: {args.config_name}')
    print(f'Mode  : {"raw" if args.raw else "calibrated"}')
    print(f'IPC   : {"socket" if args.socket else "shared memory"}')
    print(f'TCP   : {"new connection/request" if args.no_persistent else "persistent connection"}')
    print(f'Count : {args.count}, warmup: {args.warmup}')

    connection = BenchmarkConnection(args.config_name, timeout=args.timeout, persistent=not args.no_persistent)
    try:
        for _ in range(args.warmup):
            request_frame(connection, raw=args.raw, timeout=args.timeout, use_shared_memory=not args.socket)
            if args.interval > 0:
                time.sleep(args.interval)

        rows = []
        for index in range(args.count):
            row = request_frame(connection, raw=args.raw, timeout=args.timeout, use_shared_memory=not args.socket)
            rows.append(row)
            print(
                f'[{index + 1:03d}/{args.count}] '
                f'total={row["total_ms"]:.2f}ms '
                f'server={row["server_handler_ms"]:.2f}ms '
                f'lut={row["server_color_correct_ms"]:.2f}ms '
                f'ipc={row["transfer_ms"]:.2f}ms '
                f'age={row["frame_age_ms"]:.2f}ms '
                f'seq={row["seq"]}'
            )
            if index < args.count - 1 and args.interval > 0:
                time.sleep(args.interval)
    finally:
        connection.close()

    print('')
    summarize('total request latency', [row['total_ms'] for row in rows])
    summarize('client connect', [row['connect_ms'] for row in rows])
    summarize('client send request', [row['send_ms'] for row in rows])
    summarize('client wait for service header', [row['header_ms'] for row in rows])
    summarize('service handler total before reply', [row['server_handler_ms'] for row in rows])
    summarize('service frame select/cache lookup', [row['server_select_ms'] for row in rows])
    summarize('service resize/format normalize', [row['server_normalize_ms'] for row in rows])
    summarize('service color calibration', [row['server_color_correct_ms'] for row in rows])
    if not args.socket:
        summarize('service shared-memory write', [row['server_shm_write_ms'] for row in rows])
        summarize('client shared-memory open', [row['shm_open_ms'] for row in rows])
        summarize('client shared-memory copy', [row['shm_copy_ms'] for row in rows])
    summarize('image transfer/copy latency', [row['transfer_ms'] for row in rows])
    summarize('frame age when service selected it', [row['server_frame_age_at_select_ms'] for row in rows])
    summarize('returned frame age at receive end', [row['frame_age_ms'] for row in rows])
    print(f'cache hits: {sum(1 for row in rows if row["server_cache_hit"])}/{len(rows)}')


if __name__ == '__main__':
    main()
