"""
USB capture service.

Owns the capture card once, keeps the latest frame in memory, optionally shows
an OpenCV preview window, and serves frames to Alas over localhost.
"""

import argparse
import json
import socket
import socketserver
import threading
import time
import zlib

import cv2
import numpy as np

from dev_tools.usb_capture_preview import (
    DEFAULT_OUTPUT_SIZE,
    is_black,
    load_alas_config,
    mode_text,
    open_capture,
    resize_like_alas,
)


HOST = '127.0.0.1'
BASE_PORT = 27180
PORT_RANGE = 1000
FRAME_TIMEOUT = 5.0
SERVICE_WINDOW_NAME = 'Alas USB Capture Preview'


def service_port(config_name):
    return BASE_PORT + (zlib.crc32(str(config_name).encode('utf-8')) % PORT_RANGE)


def send_json(sock, payload):
    data = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')
    sock.sendall(data)


def recv_json(sock, timeout=5.0):
    sock.settimeout(timeout)
    data = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError('Connection closed')
        if chunk == b'\n':
            break
        data.extend(chunk)
    return json.loads(data.decode('utf-8'))


def request(config_name, payload, timeout=5.0):
    with socket.create_connection((HOST, service_port(config_name)), timeout=timeout) as sock:
        send_json(sock, payload)
        return recv_json(sock, timeout=timeout), sock


def ping_service(config_name, timeout=0.3):
    try:
        response, _ = request(config_name, {'cmd': 'ping'}, timeout=timeout)
        return bool(response.get('ok'))
    except Exception:
        return False


def set_preview(config_name, enabled, timeout=1.0):
    response, _ = request(config_name, {'cmd': 'preview', 'enabled': bool(enabled)}, timeout=timeout)
    return bool(response.get('ok'))


def stop_service(config_name, timeout=1.0):
    try:
        response, _ = request(config_name, {'cmd': 'stop'}, timeout=timeout)
        return bool(response.get('ok'))
    except Exception:
        return False


def get_frame(config_name, timeout=FRAME_TIMEOUT):
    with socket.create_connection((HOST, service_port(config_name)), timeout=timeout) as sock:
        send_json(sock, {'cmd': 'frame'})
        response = recv_json(sock, timeout=timeout)
        if not response.get('ok'):
            raise RuntimeError(response.get('error', 'USB capture service returned no frame'))
        size = int(response['size'])
        width = int(response['width'])
        height = int(response['height'])
        channels = int(response.get('channels', 3))
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError('Frame stream closed')
            data.extend(chunk)
        frame = np.frombuffer(data, dtype=np.uint8).reshape((height, width, channels))
        return np.ascontiguousarray(frame)


class CaptureService:
    def __init__(self, config_name='alas', preview=False, stop_event=None):
        self.config_name = config_name
        self.config_path = f'config/{config_name}.json'
        self.preview_enabled = bool(preview)
        self.stop_event = stop_event or threading.Event()
        self.lock = threading.Lock()
        self.frame_event = threading.Event()
        self.frame = None
        self.frame_time = 0.0
        self.seq = 0
        self.mode = ''
        self.measured_fps = 0.0
        self.black_ratio = 0.0
        self.preview_window_created = False
        self.server = None

    def open_capture_from_config(self):
        config = load_alas_config(self.config_path)
        device = config.get('UsbCaptureDevice', 0)
        backend = config.get('UsbCaptureBackend', 'auto')
        codec = config.get('UsbCaptureCodec', 'MJPG')
        width = int(config.get('UsbCaptureWidth', 1280))
        height = int(config.get('UsbCaptureHeight', 720))
        fps = int(config.get('UsbCaptureFps', 30))

        print(f'Opening device={device}, backend={backend}, codec={codec}, {width}x{height}@{fps}', flush=True)
        cap = open_capture(device, backend, codec, width, height, fps, buffer_size=1)
        self.mode = mode_text(cap)
        print(f'Actual mode: {self.mode}', flush=True)
        return cap

    def convert_frame(self, frame):
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return np.ascontiguousarray(resize_like_alas(frame, *DEFAULT_OUTPUT_SIZE))

    def show_preview(self, frame):
        if not self.preview_enabled:
            if self.preview_window_created:
                try:
                    cv2.destroyWindow(SERVICE_WINDOW_NAME)
                except Exception:
                    pass
                self.preview_window_created = False
            return

        if self.preview_window_created:
            try:
                if cv2.getWindowProperty(SERVICE_WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                    self.preview_enabled = False
                    self.preview_window_created = False
                    return
            except Exception:
                self.preview_enabled = False
                self.preview_window_created = False
                return

        if not self.preview_window_created:
            cv2.namedWindow(SERVICE_WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(SERVICE_WINDOW_NAME, 960, 540)
            self.preview_window_created = True

        preview = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        cv2.imshow(SERVICE_WINDOW_NAME, preview)
        key = cv2.waitKey(1) & 0xFF
        try:
            visible = cv2.getWindowProperty(SERVICE_WINDOW_NAME, cv2.WND_PROP_VISIBLE) >= 1
        except Exception:
            visible = False
        if key in (27, ord('q')) or not visible:
            self.preview_enabled = False
            self.preview_window_created = False

    def capture_loop(self):
        cap = None
        frames = 0
        black_frames = 0
        last_report = time.time()
        try:
            cap = self.open_capture_from_config()
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not (ok and frame is not None and frame.size):
                    time.sleep(0.05)
                    continue

                frames += 1
                if is_black(frame):
                    black_frames += 1
                frame = self.convert_frame(frame)
                now = time.time()
                if now - last_report >= 1.0:
                    self.measured_fps = frames / (now - last_report)
                    self.black_ratio = (black_frames / frames * 100) if frames else 0.0
                    print(
                        f'Service fps={self.measured_fps:.1f}, black={self.black_ratio:.1f}%, actual={self.mode}',
                        flush=True,
                    )
                    frames = 0
                    black_frames = 0
                    last_report = now

                with self.lock:
                    self.frame = frame
                    self.frame_time = now
                    self.seq += 1
                    self.frame_event.set()

                self.show_preview(frame)
        finally:
            if cap is not None:
                cap.release()
            if self.preview_window_created:
                try:
                    cv2.destroyWindow(SERVICE_WINDOW_NAME)
                except Exception:
                    pass

    def get_latest_frame(self):
        deadline = time.time() + FRAME_TIMEOUT
        while time.time() < deadline:
            with self.lock:
                if self.frame is not None and time.time() - self.frame_time <= FRAME_TIMEOUT:
                    return self.frame.copy(), self.frame_time, self.seq
            self.frame_event.wait(timeout=0.2)
            self.frame_event.clear()
        raise TimeoutError('Wait USB capture service frame timeout')

    def run(self):
        handler = self.make_handler()
        self.server = socketserver.ThreadingTCPServer((HOST, service_port(self.config_name)), handler)
        self.server.daemon_threads = True
        thread_server = threading.Thread(target=self.server.serve_forever, name='UsbCaptureServiceServer', daemon=True)
        thread_capture = threading.Thread(target=self.capture_loop, name='UsbCaptureServiceCapture', daemon=True)
        thread_server.start()
        thread_capture.start()
        print(f'USB capture service started: {HOST}:{service_port(self.config_name)}', flush=True)
        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
            thread_capture.join(timeout=2)
            print('USB capture service stopped', flush=True)

    def make_handler(self):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    payload = json.loads(self.rfile.readline().decode('utf-8'))
                    cmd = payload.get('cmd')
                    if cmd == 'ping':
                        send_json(self.request, {
                            'ok': True,
                            'config_name': service.config_name,
                            'seq': service.seq,
                            'mode': service.mode,
                            'preview': service.preview_enabled,
                        })
                    elif cmd == 'preview':
                        service.preview_enabled = bool(payload.get('enabled'))
                        send_json(self.request, {'ok': True, 'preview': service.preview_enabled})
                    elif cmd == 'stop':
                        service.stop_event.set()
                        send_json(self.request, {'ok': True})
                    elif cmd == 'frame':
                        frame, frame_time, seq = service.get_latest_frame()
                        data = frame.tobytes()
                        send_json(self.request, {
                            'ok': True,
                            'width': frame.shape[1],
                            'height': frame.shape[0],
                            'channels': frame.shape[2],
                            'size': len(data),
                            'time': frame_time,
                            'seq': seq,
                        })
                        self.request.sendall(data)
                    else:
                        send_json(self.request, {'ok': False, 'error': f'Unknown command: {cmd}'})
                except Exception as e:
                    try:
                        send_json(self.request, {'ok': False, 'error': str(e)})
                    except Exception:
                        pass

        return Handler


def run_service(config_name='alas', preview=False, stop_event=None):
    CaptureService(config_name=config_name, preview=preview, stop_event=stop_event).run()


def main():
    parser = argparse.ArgumentParser(description='Run the Alas USB capture service.')
    parser.add_argument('--config-name', default='alas')
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    run_service(config_name=args.config_name, preview=args.preview)


if __name__ == '__main__':
    main()
