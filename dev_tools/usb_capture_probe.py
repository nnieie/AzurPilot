"""
Probe USB/video capture device modes with OpenCV.

Examples:
    toolkit\\python.exe dev_tools\\usb_capture_probe.py --device 1 --backend dshow
    toolkit\\python.exe dev_tools\\usb_capture_probe.py --device 0 1 --backend dshow --quick
    toolkit\\python.exe dev_tools\\usb_capture_probe.py --device 1 --backend dshow --json
"""

import argparse
import json
import multiprocessing as mp
import sys
import time
from dataclasses import asdict, dataclass

import cv2


BACKENDS = {
    'auto': cv2.CAP_ANY,
    'dshow': getattr(cv2, 'CAP_DSHOW', cv2.CAP_ANY),
    'msmf': getattr(cv2, 'CAP_MSMF', cv2.CAP_ANY),
    'v4l2': getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY),
    'avfoundation': getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY),
}

COMMON_CODECS = ['MJPG', 'YUY2', 'NV12', 'H264', 'RGB3']
COMMON_RESOLUTIONS = [
    (640, 480),
    (800, 600),
    (960, 540),
    (1024, 576),
    (1280, 720),
    (1366, 768),
    (1600, 900),
    (1920, 1080),
]
COMMON_FPS = [15, 24, 25, 30, 50, 60]

QUICK_CODECS = ['MJPG', 'YUY2']
QUICK_RESOLUTIONS = [(640, 480), (1280, 720), (1920, 1080)]
QUICK_FPS = [30, 60]


@dataclass(frozen=True)
class ProbeResult:
    device: str
    backend: str
    requested_codec: str
    requested_width: int
    requested_height: int
    requested_fps: int
    actual_width: int
    actual_height: int
    actual_fps: float
    frame_width: int
    frame_height: int
    actual_codec: str


def parse_resolution(value):
    width, _, height = value.lower().partition('x')
    return int(width), int(height)


def fourcc_to_str(value):
    value = int(value)
    chars = [chr((value >> 8 * i) & 0xFF) for i in range(4)]
    text = ''.join(chars)
    if not text.strip('\x00').strip():
        return 'unknown'
    return text


def normalize_device(value):
    try:
        return int(value)
    except ValueError:
        return value


def read_frame(cap, attempts=5):
    frame = None
    ok = False
    for _ in range(attempts):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            return ok, frame
        time.sleep(0.05)
    return ok, frame


def probe_mode_worker(payload, output):
    device, backend_name, codec, width, height, fps = payload
    backend = BACKENDS[backend_name]
    cap = cv2.VideoCapture(normalize_device(device), backend)
    try:
        if not cap.isOpened():
            output.put(None)
            return

        if codec:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*codec))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)

        ok, frame = read_frame(cap)
        if not (ok and frame is not None and frame.size):
            output.put(None)
            return

        frame_height, frame_width = frame.shape[:2]
        result = ProbeResult(
            device=str(device),
            backend=backend_name,
            requested_codec=codec,
            requested_width=width,
            requested_height=height,
            requested_fps=fps,
            actual_width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            actual_height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            actual_fps=float(cap.get(cv2.CAP_PROP_FPS) or 0),
            frame_width=frame_width,
            frame_height=frame_height,
            actual_codec=fourcc_to_str(cap.get(cv2.CAP_PROP_FOURCC) or 0),
        )
        output.put(result)
    finally:
        cap.release()


def probe_mode(payload, timeout):
    output = mp.Queue(maxsize=1)
    process = mp.Process(target=probe_mode_worker, args=(payload, output), daemon=True)
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(1)
        return None
    if output.empty():
        return None
    return output.get()


def unique_results(results):
    seen = set()
    unique = []
    for item in results:
        key = (
            item.device,
            item.backend,
            item.actual_codec,
            item.actual_width,
            item.actual_height,
            round(item.actual_fps, 2),
            item.frame_width,
            item.frame_height,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def print_table(results):
    if not results:
        print('No working modes found.')
        return

    headers = [
        'dev', 'backend', 'req', 'actual prop', 'frame', 'actual codec'
    ]
    rows = []
    for item in results:
        rows.append([
            item.device,
            item.backend,
            f'{item.requested_codec} {item.requested_width}x{item.requested_height}@{item.requested_fps}',
            f'{item.actual_width}x{item.actual_height}@{item.actual_fps:.2f}',
            f'{item.frame_width}x{item.frame_height}',
            item.actual_codec,
        ])

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print('  '.join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print('  '.join('-' * width for width in widths))
    for row in rows:
        print('  '.join(value.ljust(widths[index]) for index, value in enumerate(row)))


def main():
    parser = argparse.ArgumentParser(description='Probe OpenCV capture modes.')
    parser.add_argument('--device', nargs='+', default=['0'], help='Device indexes or paths. Default: 0')
    parser.add_argument('--backend', choices=sorted(BACKENDS), default='dshow', help='OpenCV backend. Default: dshow')
    parser.add_argument('--codec', nargs='+', default=None, help='FOURCC list, e.g. MJPG YUY2 NV12')
    parser.add_argument('--resolution', nargs='+', default=None, help='Resolution list, e.g. 1280x720 1920x1080')
    parser.add_argument('--fps', nargs='+', type=int, default=None, help='FPS list, e.g. 30 60')
    parser.add_argument('--quick', action='store_true', help='Probe fewer common modes.')
    parser.add_argument('--timeout', type=float, default=4.0, help='Seconds per mode before killing the probe.')
    parser.add_argument('--json', action='store_true', help='Print JSON instead of table.')
    args = parser.parse_args()

    codecs = args.codec or (QUICK_CODECS if args.quick else COMMON_CODECS)
    resolutions = [parse_resolution(v) for v in args.resolution] if args.resolution else (
        QUICK_RESOLUTIONS if args.quick else COMMON_RESOLUTIONS
    )
    fps_list = args.fps or (QUICK_FPS if args.quick else COMMON_FPS)

    payloads = [
        (device, args.backend, codec, width, height, fps)
        for device in args.device
        for codec in codecs
        for width, height in resolutions
        for fps in fps_list
    ]

    results = []
    total = len(payloads)
    for index, payload in enumerate(payloads, 1):
        device, backend, codec, width, height, fps = payload
        print(
            f'[{index}/{total}] {device} {backend} {codec} {width}x{height}@{fps}',
            file=sys.stderr,
        )
        result = probe_mode(payload, timeout=args.timeout)
        if result is not None:
            results.append(result)

    results = unique_results(results)
    if args.json:
        print(json.dumps([asdict(item) for item in results], indent=2, ensure_ascii=False))
    else:
        print_table(results)


if __name__ == '__main__':
    mp.freeze_support()
    main()
