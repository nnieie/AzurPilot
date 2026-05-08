"""
Standalone USB/video capture preview with OpenCV.

This script does not start Alas. It opens the capture card directly, shows a
preview window, and prints the actual mode reported by the driver.

Examples:
    toolkit\\python.exe dev_tools\\usb_capture_preview.py
    toolkit\\python.exe dev_tools\\usb_capture_preview.py --device 1 --codec YUY2 --width 1280 --height 720 --fps 30
    toolkit\\python.exe dev_tools\\usb_capture_preview.py --device 1 --codec MJPG --width 1920 --height 1080 --fps 60
"""

import argparse
import json
import os
import platform
import time

import cv2
import numpy as np


BACKENDS = {
    'any': cv2.CAP_ANY,
    'auto': None,
    'dshow': getattr(cv2, 'CAP_DSHOW', cv2.CAP_ANY),
    'msmf': getattr(cv2, 'CAP_MSMF', cv2.CAP_ANY),
    'v4l2': getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY),
    'avfoundation': getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY),
}

WINDOW_NAME = 'Alas USB Capture Standalone Preview'
DEFAULT_OUTPUT_SIZE = (1280, 720)
DEFAULT_PREVIEW_SIZE = (960, 540)
PREVIEW_INTERPOLATIONS = {
    'nearest': cv2.INTER_NEAREST,
    'linear': cv2.INTER_LINEAR,
    'cubic': cv2.INTER_CUBIC,
    'area': cv2.INTER_AREA,
    'lanczos4': cv2.INTER_LANCZOS4,
}


def normalize_device(value):
    value = str(value).strip()
    try:
        return int(value)
    except ValueError:
        return value


def backend_value(name):
    name = str(name).strip().lower()
    if name != 'auto':
        return BACKENDS.get(name, cv2.CAP_ANY)
    system = platform.system().lower()
    if system == 'windows':
        return getattr(cv2, 'CAP_DSHOW', cv2.CAP_ANY)
    if system == 'darwin':
        return getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY)
    return getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY)


def fourcc_value(codec):
    codec = str(codec).strip().upper()
    if codec in ('', 'AUTO', 'DEFAULT'):
        return None
    if codec == 'MJPEG':
        codec = 'MJPG'
    if len(codec) != 4:
        raise ValueError(f'Invalid codec: {codec}. Expected a 4-character FOURCC, e.g. MJPG or YUY2.')
    return cv2.VideoWriter_fourcc(*codec)


def fourcc_name(value):
    try:
        value = int(value)
        text = ''.join(chr((value >> 8 * i) & 0xFF) for i in range(4))
        if text.strip('\x00 '):
            return text
    except Exception:
        pass
    return 'unknown'


def parse_size(value):
    width, _, height = str(value).lower().partition('x')
    if not width or not height:
        raise argparse.ArgumentTypeError('Expected size like 1280x720')
    return int(width), int(height)


def resize_like_alas(image, width, height):
    current_height, current_width = image.shape[:2]
    if current_width == width and current_height == height:
        return image

    if (current_width, current_height, width, height) == (640, 480, 1280, 720):
        return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

    target_ratio = width / height
    current_ratio = current_width / current_height
    if current_ratio > target_ratio:
        crop_width = int(current_height * target_ratio)
        left = (current_width - crop_width) // 2
        image = image[:, left:left + crop_width]
    elif current_ratio < target_ratio:
        crop_height = int(current_width / target_ratio)
        top = (current_height - crop_height) // 2
        image = image[top:top + crop_height, :]

    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def preview_interpolation(image, width, height, interpolation='auto'):
    interpolation = str(interpolation or 'auto').strip().lower()
    if interpolation in PREVIEW_INTERPOLATIONS:
        return PREVIEW_INTERPOLATIONS[interpolation]

    current_height, current_width = image.shape[:2]
    if width < current_width or height < current_height:
        return cv2.INTER_AREA
    return cv2.INTER_LINEAR


def resize_for_preview(image, width, height, interpolation='auto'):
    current_height, current_width = image.shape[:2]
    if current_width == width and current_height == height:
        return image
    return cv2.resize(image, (width, height), interpolation=preview_interpolation(image, width, height, interpolation))


def fit_for_preview(image, width, height, lock_aspect=True, interpolation='auto'):
    current_height, current_width = image.shape[:2]
    if not lock_aspect:
        return resize_for_preview(image, width, height, interpolation), (0, 0, width, height)

    scale = min(width / current_width, height / current_height)
    content_width = max(1, int(round(current_width * scale)))
    content_height = max(1, int(round(current_height * scale)))
    left = max(0, (width - content_width) // 2)
    top = max(0, (height - content_height) // 2)
    resized = resize_for_preview(image, content_width, content_height, interpolation)
    if content_width == width and content_height == height:
        return resized, (0, 0, width, height)

    canvas = np.zeros((height, width, image.shape[2]), dtype=image.dtype)
    canvas[top:top + content_height, left:left + content_width] = resized
    return canvas, (left, top, content_width, content_height)


def get_window_image_size(name, fallback=DEFAULT_PREVIEW_SIZE):
    try:
        _, _, width, height = cv2.getWindowImageRect(name)
        if width > 0 and height > 0:
            return int(width), int(height)
    except Exception:
        pass
    return fallback


def load_alas_config(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    return data.get('Alas', {}).get('Emulator', {})


def choose(value, config, key, fallback):
    if value is not None:
        return value
    return config.get(key, fallback)


def open_capture(device, backend, codec, width, height, fps, buffer_size):
    backend_id = backend_value(backend)
    cap = cv2.VideoCapture(normalize_device(device), backend_id)
    if not cap.isOpened():
        raise RuntimeError(f'Unable to open capture device: {device}, backend={backend}')

    codec_id = fourcc_value(codec)
    if codec_id is not None:
        cap.set(cv2.CAP_PROP_FOURCC, codec_id)
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    if fps:
        cap.set(cv2.CAP_PROP_FPS, int(fps))
    if buffer_size is not None:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, int(buffer_size))

    return cap


def mode_text(cap):
    return (
        f'{fourcc_name(cap.get(cv2.CAP_PROP_FOURCC) or 0)} '
        f'{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)}x'
        f'{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)} '
        f'@ {cap.get(cv2.CAP_PROP_FPS) or 0:.2f}fps'
    )


def is_black(frame):
    return frame is not None and frame.size and sum(cv2.mean(frame)[:3]) < 1


def put_overlay(frame, text):
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 32), (0, 0, 0), -1)
    cv2.putText(frame, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def is_window_visible(name):
    try:
        return cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE) >= 1
    except Exception:
        return False


def parse_args():
    parser = argparse.ArgumentParser(description='Open a standalone OpenCV preview for a USB capture card.')
    parser.add_argument('--config', default='config/alas.json', help='Read defaults from Alas config. Default: config/alas.json')
    parser.add_argument('--device', default=None, help='Device index or path. Default: value from config, then 0')
    parser.add_argument('--backend', choices=sorted(BACKENDS), default=None, help='OpenCV backend. Default: value from config, then auto')
    parser.add_argument('--codec', default=None, help='FOURCC codec, e.g. MJPG, YUY2, default. Default: value from config, then MJPG')
    parser.add_argument('--width', type=int, default=None, help='Requested input width. Default: value from config, then 1280')
    parser.add_argument('--height', type=int, default=None, help='Requested input height. Default: value from config, then 720')
    parser.add_argument('--fps', type=int, default=None, help='Requested FPS. Default: value from config, then 30')
    parser.add_argument('--buffer-size', type=int, default=1, help='OpenCV capture buffer size. Default: 1')
    parser.add_argument('--output-size', type=parse_size, default=DEFAULT_OUTPUT_SIZE, help='Preview output size. Default: 1280x720')
    parser.add_argument('--raw', action='store_true', help='Show raw frame without Alas-style resize/crop.')
    parser.add_argument('--no-overlay', action='store_true', help='Do not draw mode/FPS text over the preview.')
    parser.add_argument('--unlock-aspect', action='store_true', help='Stretch preview to fill the window instead of keeping aspect ratio.')
    parser.add_argument('--preview-fps', type=float, default=None, help='Limit preview redraw FPS. Default: value from config, then 30.')
    parser.add_argument(
        '--preview-interpolation',
        choices=['auto', *sorted(PREVIEW_INTERPOLATIONS)],
        default=None,
        help='Preview resize interpolation. Default: value from config, then linear.',
    )
    parser.add_argument('--save-dir', default='.', help='Directory for snapshots saved with s. Default: current directory')
    return parser.parse_args()


def main():
    args = parse_args()
    run_preview_from_args(args)


def run_preview(config_name='alas', stop_event=None):
    args = argparse.Namespace(
        config=os.path.join('config', f'{config_name}.json'),
        device=None,
        backend=None,
        codec=None,
        width=None,
        height=None,
        fps=None,
        buffer_size=1,
        output_size=DEFAULT_OUTPUT_SIZE,
        raw=False,
        no_overlay=False,
        unlock_aspect=False,
        preview_fps=None,
        preview_interpolation=None,
        save_dir='.',
    )
    run_preview_from_args(args, stop_event=stop_event)


def run_preview_from_args(args, stop_event=None):
    config = load_alas_config(args.config)

    device = choose(args.device, config, 'UsbCaptureDevice', 0)
    backend = choose(args.backend, config, 'UsbCaptureBackend', 'auto')
    codec = choose(args.codec, config, 'UsbCaptureCodec', 'MJPG')
    width = int(choose(args.width, config, 'UsbCaptureWidth', 1280))
    height = int(choose(args.height, config, 'UsbCaptureHeight', 720))
    fps = int(choose(args.fps, config, 'UsbCaptureFps', 30))
    lock_aspect = bool(config.get('UsbCaptureLockPreviewAspect', True)) and not args.unlock_aspect
    preview_size = (
        max(1, int(config.get('UsbCapturePreviewWidth', DEFAULT_PREVIEW_SIZE[0]))),
        max(1, int(config.get('UsbCapturePreviewHeight', DEFAULT_PREVIEW_SIZE[1]))),
    )
    preview_fps = float(choose(args.preview_fps, config, 'UsbCapturePreviewFps', 30))
    preview_interval = 0.0 if preview_fps <= 0 else 1 / preview_fps
    preview_interpolation_name = choose(args.preview_interpolation, config, 'UsbCapturePreviewInterpolation', 'linear')

    print(f'Opening device={device}, backend={backend}, codec={codec}, {width}x{height}@{fps}')
    cap = open_capture(device, backend, codec, width, height, fps, args.buffer_size)
    print(f'Actual mode: {mode_text(cap)}')
    print('Keys: s save snapshot, r reconnect, space pause. Close the window to quit.')

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, *preview_size)

    frames = 0
    black_frames = 0
    last_report = time.time()
    measured_fps = 0.0
    paused = False
    last_frame = None
    last_preview_time = 0.0

    try:
        while not (stop_event is not None and stop_event.is_set()):
            if not paused:
                ok, frame = cap.read()
                if not (ok and frame is not None and frame.size):
                    print('Read failed, retrying...')
                    time.sleep(0.1)
                    cv2.waitKey(1)
                    if not is_window_visible(WINDOW_NAME):
                        break
                    continue
                last_frame = frame
                frames += 1
                if is_black(frame):
                    black_frames += 1
            elif last_frame is None:
                key = cv2.waitKey(30) & 0xFF
                if not is_window_visible(WINDOW_NAME):
                    break
                if key == ord(' '):
                    paused = False
                continue

            now = time.time()
            if now - last_report >= 1.0:
                measured_fps = frames / (now - last_report)
                black_ratio = (black_frames / frames * 100) if frames else 0
                print(f'Preview fps={measured_fps:.1f}, black={black_ratio:.1f}%, actual={mode_text(cap)}')
                frames = 0
                black_frames = 0
                last_report = now

            if preview_interval > 0 and now - last_preview_time < preview_interval:
                key = cv2.waitKey(1) & 0xFF
                if not is_window_visible(WINDOW_NAME):
                    break
                if key == ord(' '):
                    paused = not paused
                continue
            last_preview_time = now

            preview = last_frame.copy()
            if not args.raw:
                preview = resize_like_alas(preview, *args.output_size)
            preview, _ = fit_for_preview(
                preview,
                *get_window_image_size(WINDOW_NAME),
                lock_aspect=lock_aspect,
                interpolation=preview_interpolation_name,
            )
            if not args.no_overlay:
                text = f'{mode_text(cap)} | preview {measured_fps:.1f}fps'
                if paused:
                    text += ' | PAUSED'
                put_overlay(preview, text)
            cv2.imshow(WINDOW_NAME, preview)

            key = cv2.waitKey(1) & 0xFF
            if not is_window_visible(WINDOW_NAME):
                break
            if key == ord(' '):
                paused = not paused
            elif key == ord('s'):
                os.makedirs(args.save_dir, exist_ok=True)
                path = os.path.join(args.save_dir, f'usb_capture_preview_{int(time.time() * 1000)}.png')
                cv2.imwrite(path, preview)
                print(f'Saved {path}')
            elif key == ord('r'):
                print('Reconnecting...')
                cap.release()
                cap = open_capture(device, backend, codec, width, height, fps, args.buffer_size)
                print(f'Actual mode: {mode_text(cap)}')
    finally:
        cap.release()
        try:
            cv2.destroyWindow(WINDOW_NAME)
        except Exception:
            pass


if __name__ == '__main__':
    main()
