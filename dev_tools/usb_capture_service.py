"""
USB capture service.

Owns the capture card once, keeps the latest frame in memory, optionally shows
an OpenCV preview window, and serves frames to Alas over localhost.
"""

import argparse
import ctypes
import json
import os
import socket
import socketserver
import threading
import time
import zlib
from multiprocessing import shared_memory

import cv2
import numpy as np

from dev_tools.usb_capture_preview import (
    DEFAULT_OUTPUT_SIZE,
    DEFAULT_PREVIEW_SIZE,
    fit_for_preview,
    get_window_image_size,
    is_black,
    load_alas_config,
    mode_text,
    open_capture,
    resize_like_alas,
)


HOST = '127.0.0.1'
BASE_PORT = 27180
CONTROL_BASE_PORT = 28180
PORT_RANGE = 1000
FRAME_TIMEOUT = 5.0
SERVICE_WINDOW_NAME = 'Alas USB Capture Preview'
PREVIEW_TAP_DISTANCE = 10
PREVIEW_MOVE_INTERVAL = 0.016
USB_LUT_ACCEL_DLL = os.path.join(os.path.dirname(__file__), 'usb_capture_lut_accel.dll')
_USB_LUT_ACCEL = None
_FRAME_CLIENTS = {}
_FRAME_CLIENTS_LOCK = threading.Lock()

ANDROID_KEY_BACK = 4
ANDROID_KEY_DPAD_UP = 19
ANDROID_KEY_DPAD_DOWN = 20
ANDROID_KEY_DPAD_LEFT = 21
ANDROID_KEY_DPAD_RIGHT = 22
ANDROID_KEY_TAB = 61
ANDROID_KEY_ENTER = 66
ANDROID_KEY_DEL = 67


def service_port(config_name):
    return BASE_PORT + (zlib.crc32(str(config_name).encode('utf-8')) % PORT_RANGE)


def color_correction_path(config_name):
    return os.path.join('config', 'usb_color', f'{config_name}.json')


def legacy_color_correction_path(config_name):
    return os.path.join('config', f'{config_name}.usb_color.json')


class UsbLutAccel:
    def __init__(self, path):
        if os.name != 'nt':
            raise RuntimeError('USB LUT C accelerator is Windows-only')
        if not os.path.exists(path):
            raise RuntimeError(
                f'USB LUT C accelerator not found: {path}. '
                r'Run dev_tools\build_usb_capture_lut_accel.bat first.'
            )
        self.dll = ctypes.CDLL(path)
        pointer = ctypes.POINTER(ctypes.c_uint8)
        argtypes = [pointer, pointer, ctypes.c_int, ctypes.c_int, pointer, ctypes.c_int]
        self.dll.usb_lut3d_apply_rgb.argtypes = argtypes
        self.dll.usb_lut3d_apply_rgb.restype = ctypes.c_int
        self.dll.usb_lut3d_apply_bgr.argtypes = argtypes
        self.dll.usb_lut3d_apply_bgr.restype = ctypes.c_int

    @staticmethod
    def _pointer(array):
        return array.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))

    def apply(self, func, frame, flat_lut, levels):
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError('USB LUT accelerator expects a uint8 HxWx3 frame')
        if flat_lut.dtype != np.uint8 or flat_lut.ndim != 2 or flat_lut.shape[1] != 3:
            raise ValueError('USB LUT accelerator expects a uint8 Nx3 LUT')
        frame = np.ascontiguousarray(frame)
        flat_lut = np.ascontiguousarray(flat_lut)
        output = np.empty_like(frame)
        height, width = frame.shape[:2]
        code = func(
            self._pointer(frame),
            self._pointer(output),
            int(width),
            int(height),
            self._pointer(flat_lut),
            int(levels),
        )
        if code != 0:
            raise RuntimeError(f'USB LUT C accelerator failed with code {code}')
        return output

    def apply_rgb(self, frame, flat_lut, levels):
        return self.apply(self.dll.usb_lut3d_apply_rgb, frame, flat_lut, levels)

    def apply_bgr(self, frame, flat_lut, levels):
        return self.apply(self.dll.usb_lut3d_apply_bgr, frame, flat_lut, levels)


def get_usb_lut_accel():
    global _USB_LUT_ACCEL
    if _USB_LUT_ACCEL is None:
        _USB_LUT_ACCEL = UsbLutAccel(USB_LUT_ACCEL_DLL)
    return _USB_LUT_ACCEL


def load_usb_color_correction(config_name, use_c_accel=True):
    path = color_correction_path(config_name)
    legacy_path = legacy_color_correction_path(config_name)
    if not os.path.exists(path) and os.path.exists(legacy_path):
        path = legacy_path
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    if not data.get('enabled', True):
        return None
    model = data.get('model', 'rgb_linear')
    if model == 'lut3d':
        if use_c_accel:
            get_usb_lut_accel()
        lut = np.asarray(data['lut'], dtype=np.uint8)
        if lut.ndim != 4 or lut.shape[0] != lut.shape[1] or lut.shape[1] != lut.shape[2] or lut.shape[3] != 3:
            raise ValueError(f'Invalid USB color 3D LUT file: {path}')
        fast_lut = build_fast_lut3d(lut)
        return {
            'path': path,
            'model': model,
            'lut': lut,
            'fast_lut': fast_lut,
            'fast_lut_flat': fast_lut.reshape(-1, 3),
            'use_c_accel': bool(use_c_accel),
        }
    if model == 'channel_lut':
        luts = np.asarray(data['luts'], dtype=np.uint8)
        if luts.shape != (3, 256):
            raise ValueError(f'Invalid USB color LUT file: {path}')
        return {
            'path': path,
            'model': model,
            'luts': luts,
        }
    if model == 'rgb_linear':
        matrix = np.asarray(data['matrix'], dtype=np.float32)
        bias = np.asarray(data['bias'], dtype=np.float32)
        if matrix.shape != (3, 3) or bias.shape != (3,):
            raise ValueError(f'Invalid USB color correction file: {path}')
        return {
            'path': path,
            'model': model,
            'matrix': matrix,
            'bias': bias,
        }
    raise ValueError(f'Unsupported USB color correction model: {model}')


def apply_usb_color_correction(frame, correction):
    if correction is None:
        return frame
    if correction.get('model') == 'lut3d':
        return apply_fast_lut3d(
            frame,
            correction['fast_lut'],
            correction.get('fast_lut_flat'),
            use_c_accel=correction.get('use_c_accel', True),
        )
    if correction.get('model') == 'channel_lut':
        corrected = np.empty_like(frame)
        for channel in range(3):
            corrected[:, :, channel] = correction['luts'][channel][frame[:, :, channel]]
        return np.ascontiguousarray(corrected)
    corrected = frame.astype(np.float32) @ correction['matrix'] + correction['bias']
    return np.ascontiguousarray(np.clip(corrected, 0, 255).astype(np.uint8))


def apply_lut3d(frame, lut):
    size = lut.shape[0]
    scaled = frame.astype(np.float32) * ((size - 1) / 255.0)
    index0 = np.floor(scaled).astype(np.int16)
    frac = scaled - index0
    index1 = np.minimum(index0 + 1, size - 1)

    r0, g0, b0 = index0[:, :, 0], index0[:, :, 1], index0[:, :, 2]
    r1, g1, b1 = index1[:, :, 0], index1[:, :, 1], index1[:, :, 2]
    tr, tg, tb = frac[:, :, 0:1], frac[:, :, 1:2], frac[:, :, 2:3]

    c000 = lut[r0, g0, b0].astype(np.float32)
    c100 = lut[r1, g0, b0].astype(np.float32)
    c010 = lut[r0, g1, b0].astype(np.float32)
    c110 = lut[r1, g1, b0].astype(np.float32)
    c001 = lut[r0, g0, b1].astype(np.float32)
    c101 = lut[r1, g0, b1].astype(np.float32)
    c011 = lut[r0, g1, b1].astype(np.float32)
    c111 = lut[r1, g1, b1].astype(np.float32)

    c00 = c000 * (1 - tr) + c100 * tr
    c10 = c010 * (1 - tr) + c110 * tr
    c01 = c001 * (1 - tr) + c101 * tr
    c11 = c011 * (1 - tr) + c111 * tr
    c0 = c00 * (1 - tg) + c10 * tg
    c1 = c01 * (1 - tg) + c11 * tg
    corrected = c0 * (1 - tb) + c1 * tb
    return np.ascontiguousarray(np.clip(np.rint(corrected), 0, 255).astype(np.uint8))


def build_fast_lut3d(lut, bits=6):
    """Precompute a nearest-grid RGB lookup table.

    Runtime trilinear interpolation over a 33^3 LUT is too expensive at
    1280x720. A 6-bit-per-channel table needs less than 1 MiB and turns
    correction into one integer index plus one gather per pixel.
    """
    levels = 1 << bits
    axis = np.linspace(0, 255, levels, dtype=np.uint8)
    rr, gg, bb = np.meshgrid(axis, axis, axis, indexing='ij')
    samples = np.stack([rr, gg, bb], axis=-1).reshape(-1, 1, 3)
    corrected = apply_lut3d(samples, lut)
    return corrected.reshape(levels, levels, levels, 3)


def apply_fast_lut3d_numpy(frame, fast_lut, flat_lut=None):
    levels = fast_lut.shape[0]
    shift = 8 - int(np.log2(levels))
    bits = 8 - shift
    if flat_lut is None:
        flat_lut = fast_lut.reshape(-1, 3)
    index = frame[:, :, 0].astype(np.uint32)
    index >>= shift
    index <<= bits * 2
    channel = frame[:, :, 1].astype(np.uint32)
    channel >>= shift
    channel <<= bits
    index |= channel
    channel = frame[:, :, 2].astype(np.uint32)
    channel >>= shift
    index |= channel
    return np.ascontiguousarray(flat_lut[index])


def apply_fast_lut3d_bgr_numpy(frame, fast_lut, flat_lut=None):
    levels = fast_lut.shape[0]
    shift = 8 - int(np.log2(levels))
    bits = 8 - shift
    if flat_lut is None:
        flat_lut = fast_lut.reshape(-1, 3)
    index = frame[:, :, 2].astype(np.uint32)
    index >>= shift
    index <<= bits * 2
    channel = frame[:, :, 1].astype(np.uint32)
    channel >>= shift
    channel <<= bits
    index |= channel
    channel = frame[:, :, 0].astype(np.uint32)
    channel >>= shift
    index |= channel
    return np.ascontiguousarray(flat_lut[index])


def apply_fast_lut3d(frame, fast_lut, flat_lut=None, use_c_accel=True):
    levels = fast_lut.shape[0]
    if flat_lut is None:
        flat_lut = fast_lut.reshape(-1, 3)
    if not use_c_accel:
        return apply_fast_lut3d_numpy(frame, fast_lut, flat_lut)
    return get_usb_lut_accel().apply_rgb(frame, flat_lut, levels)


def apply_fast_lut3d_bgr(frame, fast_lut, flat_lut=None, use_c_accel=True):
    levels = fast_lut.shape[0]
    if flat_lut is None:
        flat_lut = fast_lut.reshape(-1, 3)
    if not use_c_accel:
        return apply_fast_lut3d_bgr_numpy(frame, fast_lut, flat_lut)
    return get_usb_lut_accel().apply_bgr(frame, flat_lut, levels)


def control_port(config_name):
    return CONTROL_BASE_PORT + (zlib.crc32(str(config_name).encode('utf-8')) % PORT_RANGE)


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


def control_request(config_name, payload, timeout=0.5):
    with socket.create_connection((HOST, control_port(config_name)), timeout=timeout) as sock:
        send_json(sock, payload)
        return recv_json(sock, timeout=timeout)


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
        close_frame_client(config_name)
        return bool(response.get('ok'))
    except Exception:
        return False


class UsbCaptureFrameClient:
    def __init__(self, config_name, timeout=FRAME_TIMEOUT):
        self.config_name = config_name
        self.timeout = timeout
        self.sock = None
        self.lock = threading.Lock()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def connect(self):
        if self.sock is None:
            self.sock = socket.create_connection((HOST, service_port(self.config_name)), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        return self.sock

    def request_frame(self, raw=False, use_shared_memory=True, return_metadata=False):
        with self.lock:
            try:
                return self._request_frame(raw=raw, use_shared_memory=use_shared_memory, return_metadata=return_metadata)
            except Exception:
                self.close()
                return self._request_frame(raw=raw, use_shared_memory=use_shared_memory, return_metadata=return_metadata)

    def _request_frame(self, raw=False, use_shared_memory=True, return_metadata=False):
        sock = self.connect()
        frame, response = receive_frame_from_socket(
            sock,
            raw=raw,
            use_shared_memory=use_shared_memory,
            timeout=self.timeout,
            return_metadata=True,
            profile=return_metadata,
        )
        return (frame, response) if return_metadata else frame


def get_frame_client(config_name, timeout=FRAME_TIMEOUT):
    key = (str(config_name), float(timeout))
    with _FRAME_CLIENTS_LOCK:
        client = _FRAME_CLIENTS.get(key)
        if client is None:
            client = UsbCaptureFrameClient(config_name, timeout=timeout)
            _FRAME_CLIENTS[key] = client
        return client


def close_frame_client(config_name=None):
    with _FRAME_CLIENTS_LOCK:
        items = list(_FRAME_CLIENTS.items())
        for key, client in items:
            if config_name is None or key[0] == str(config_name):
                client.close()
                _FRAME_CLIENTS.pop(key, None)


def receive_frame_from_socket(
    sock,
    raw=False,
    use_shared_memory=True,
    timeout=FRAME_TIMEOUT,
    return_metadata=False,
    profile=False,
):
    if use_shared_memory:
        cmd = 'frame_shm_raw' if raw else 'frame_shm'
    else:
        cmd = 'frame_raw' if raw else 'frame'
    send_json(sock, {'cmd': cmd, 'profile': bool(profile)})
    response = recv_json(sock, timeout=timeout)
    if not response.get('ok'):
        raise RuntimeError(response.get('error', 'USB capture service returned no frame'))
    size = int(response['size'])
    width = int(response['width'])
    height = int(response['height'])
    channels = int(response.get('channels', 3))
    if use_shared_memory:
        shm = shared_memory.SharedMemory(name=response['shm_name'], track=False)
        try:
            array = np.ndarray((height, width, channels), dtype=np.uint8, buffer=shm.buf[:size])
            frame = array.copy()
            del array
        finally:
            shm.close()
    else:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError('Frame stream closed')
            data.extend(chunk)
        frame = np.frombuffer(data, dtype=np.uint8).reshape((height, width, channels))
        frame = np.ascontiguousarray(frame)
    return (frame, response) if return_metadata else (frame, None)


def get_frame(
    config_name,
    timeout=FRAME_TIMEOUT,
    raw=False,
    use_shared_memory=True,
    return_metadata=False,
    persistent=True,
):
    if persistent:
        return get_frame_client(config_name, timeout=timeout).request_frame(
            raw=raw,
            use_shared_memory=use_shared_memory,
            return_metadata=return_metadata,
        )

    with socket.create_connection((HOST, service_port(config_name)), timeout=timeout) as sock:
        frame, response = receive_frame_from_socket(
            sock,
            raw=raw,
            use_shared_memory=use_shared_memory,
            timeout=timeout,
            return_metadata=True,
            profile=return_metadata,
        )
        return (frame, response) if return_metadata else frame


class CaptureService:
    def __init__(self, config_name='alas', preview=False, stop_event=None):
        self.config_name = config_name
        self.config_path = f'config/{config_name}.json'
        self.preview_enabled = bool(preview)
        self.stop_event = stop_event or threading.Event()
        self.lock = threading.Lock()
        self.frame_event = threading.Event()
        self.correction_event = threading.Event()
        self.frame = None
        self.capture_frame = None
        self.raw_frame = None
        self.raw_seq = 0
        self.corrected_frame = None
        self.corrected_seq = 0
        self.frame_time = 0.0
        self.seq = 0
        self.mode = ''
        self.measured_fps = 0.0
        self.black_ratio = 0.0
        self.preview_window_created = False
        self.server = None
        self.shm_lock = threading.Lock()
        self.shm = None
        self.shm_size = 0
        self.shm_seq = 0
        self.mouse_down = None
        self.last_control_warning = 0.0
        self.control_lock = threading.Lock()
        self.control_device = None
        self.control_preload_started = False
        self.realtime_touch_active = False
        self.last_mouse_move_time = 0.0
        self.remote_control_retry_time = 0.0
        self.color_correction = None
        self.usb_capture_c_accel = True
        self.recent_frame_request_time = 0.0
        self.precorrect_interval = None
        self.last_precorrect_time = 0.0
        self.preview_display_size = DEFAULT_PREVIEW_SIZE
        self.preview_content_rect = (0, 0, *DEFAULT_PREVIEW_SIZE)
        self.preview_initial_size = DEFAULT_PREVIEW_SIZE
        self.preview_lock_aspect = True
        self.preview_interval = 1 / 30
        self.preview_interpolation = 'linear'
        self.last_preview_time = 0.0

    def open_capture_from_config(self):
        config = load_alas_config(self.config_path)
        device = config.get('UsbCaptureDevice', 0)
        backend = config.get('UsbCaptureBackend', 'auto')
        codec = config.get('UsbCaptureCodec', 'MJPG')
        width = int(config.get('UsbCaptureWidth', 1280))
        height = int(config.get('UsbCaptureHeight', 720))
        fps = int(config.get('UsbCaptureFps', 30))
        self.usb_capture_c_accel = bool(config.get('UsbCaptureCAccel', True))
        self.preview_lock_aspect = bool(config.get('UsbCaptureLockPreviewAspect', True))
        self.preview_initial_size = (
            max(1, int(config.get('UsbCapturePreviewWidth', DEFAULT_PREVIEW_SIZE[0]))),
            max(1, int(config.get('UsbCapturePreviewHeight', DEFAULT_PREVIEW_SIZE[1]))),
        )
        preview_fps = float(config.get('UsbCapturePreviewFps', 30))
        self.preview_interval = 0.0 if preview_fps <= 0 else 1 / preview_fps
        self.preview_interpolation = str(config.get('UsbCapturePreviewInterpolation', 'linear'))
        precorrect_fps = float(config.get('UsbCapturePreCorrectFps', 0))
        self.precorrect_interval = None if precorrect_fps <= 0 else 1 / precorrect_fps

        print(f'Opening device={device}, backend={backend}, codec={codec}, {width}x{height}@{fps}', flush=True)
        cap = open_capture(device, backend, codec, width, height, fps, buffer_size=1)
        self.mode = mode_text(cap)
        print(f'Actual mode: {self.mode}', flush=True)
        self.color_correction = load_usb_color_correction(self.config_name, use_c_accel=self.usb_capture_c_accel)
        if self.color_correction is not None:
            print(f'USB color correction loaded: {self.color_correction["path"]}', flush=True)
        return cap

    def convert_frame(self, frame):
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return np.ascontiguousarray(resize_like_alas(frame, *DEFAULT_OUTPUT_SIZE))

    def normalize_capture_frame_bgr(self, frame):
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        return np.ascontiguousarray(resize_like_alas(frame, *DEFAULT_OUTPUT_SIZE))

    def correct_capture_frame(self, frame, profile=None):
        start = time.perf_counter()
        if self.color_correction is not None and self.color_correction.get('model') == 'lut3d':
            frame = self.normalize_capture_frame_bgr(frame)
            normalized = time.perf_counter()
            corrected = apply_fast_lut3d_bgr(
                frame,
                self.color_correction['fast_lut'],
                self.color_correction.get('fast_lut_flat'),
                use_c_accel=self.color_correction.get('use_c_accel', True),
            )
            if profile is not None:
                end = time.perf_counter()
                profile['normalize_ms'] = (normalized - start) * 1000
                profile['color_correct_ms'] = (end - normalized) * 1000
            return corrected
        frame = self.convert_frame(frame)
        normalized = time.perf_counter()
        corrected = apply_usb_color_correction(frame, self.color_correction)
        if profile is not None:
            end = time.perf_counter()
            profile['normalize_ms'] = (normalized - start) * 1000
            profile['color_correct_ms'] = (end - normalized) * 1000
        return corrected

    def shared_memory_frame(self, frame, profile=None):
        size = int(frame.nbytes)
        start = time.perf_counter()
        with self.shm_lock:
            if self.shm is None or self.shm_size < size:
                if self.shm is not None:
                    try:
                        self.shm.close()
                        self.shm.unlink()
                    except FileNotFoundError:
                        pass
                self.shm = shared_memory.SharedMemory(create=True, size=size, track=True)
                self.shm_size = size
            self.shm.buf[:size] = frame.reshape(-1)
            self.shm_seq += 1
            if profile is not None:
                profile['shm_write_ms'] = (time.perf_counter() - start) * 1000
            return self.shm.name, self.shm_seq

    def shared_memory_close(self):
        with self.shm_lock:
            if self.shm is not None:
                try:
                    self.shm.close()
                    self.shm.unlink()
                except FileNotFoundError:
                    pass
                self.shm = None
                self.shm_size = 0

    def show_preview(self, frame, rgb=True):
        if not self.preview_enabled:
            if self.preview_window_created:
                try:
                    cv2.destroyWindow(SERVICE_WINDOW_NAME)
                except Exception:
                    pass
                self.preview_window_created = False
            return

        self.preload_control_device()

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
            cv2.resizeWindow(SERVICE_WINDOW_NAME, *self.preview_initial_size)
            cv2.setMouseCallback(SERVICE_WINDOW_NAME, self.on_mouse)
            self.preview_window_created = True

        now = time.time()
        if self.preview_interval > 0 and now - self.last_preview_time < self.preview_interval:
            key = cv2.waitKeyEx(1)
            if key != -1:
                self.on_key(key)
            return
        self.last_preview_time = now

        if rgb:
            preview = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        else:
            preview = self.resize_preview_bgr(frame)
        self.preview_display_size = get_window_image_size(SERVICE_WINDOW_NAME)
        preview, self.preview_content_rect = fit_for_preview(
            preview,
            *self.preview_display_size,
            lock_aspect=self.preview_lock_aspect,
            interpolation=self.preview_interpolation,
        )
        cv2.imshow(SERVICE_WINDOW_NAME, preview)
        key = cv2.waitKeyEx(1)
        try:
            visible = cv2.getWindowProperty(SERVICE_WINDOW_NAME, cv2.WND_PROP_VISIBLE) >= 1
        except Exception:
            visible = False
        if not visible:
            self.preview_enabled = False
            try:
                cv2.destroyWindow(SERVICE_WINDOW_NAME)
            except Exception:
                pass
            self.preview_window_created = False
        elif key != -1:
            self.on_key(key)

    def resize_preview_bgr(self, frame):
        height, width = frame.shape[:2]
        target_width, target_height = DEFAULT_OUTPUT_SIZE
        if width == target_width and height == target_height:
            return frame
        return resize_like_alas(frame, target_width, target_height)

    def send_control(self, payload):
        remote_error = None
        now = time.time()
        if now >= self.remote_control_retry_time:
            try:
                response = control_request(self.config_name, payload, timeout=0.2)
                if response.get('ok'):
                    self.remote_control_retry_time = 0.0
                    return True
                remote_error = response.get('error', 'unknown error')
            except Exception:
                self.remote_control_retry_time = now + 2.0

        if remote_error is not None:
            if now - self.last_control_warning > 5:
                print(f'USB preview control unavailable: {remote_error}', flush=True)
                self.last_control_warning = now
            return False

        try:
            response = self.handle_local_control(payload)
            if response.get('ok'):
                return True
            error = response.get('error', 'unknown error')
        except Exception as e:
            error = str(e)

        now = time.time()
        if now - self.last_control_warning > 5:
            print(f'USB preview control unavailable: {error}', flush=True)
            self.last_control_warning = now
        return False

    def preload_control_device(self):
        if self.control_preload_started:
            return
        self.control_preload_started = True
        thread = threading.Thread(
            target=self.preload_control_device_worker,
            name='UsbCapturePreviewControlPreload',
            daemon=True,
        )
        thread.start()

    def preload_control_device_worker(self):
        try:
            self.get_control_device()
            print('USB preview control ready', flush=True)
        except Exception as e:
            self.control_preload_started = False
            now = time.time()
            if now - self.last_control_warning > 5:
                print(f'USB preview control preload failed: {e}', flush=True)
                self.last_control_warning = now

    def get_control_device(self):
        with self.control_lock:
            if self.control_device is not None:
                return self.control_device

            from module.config.config import AzurLaneConfig
            from module.device.device import Device

            config = AzurLaneConfig(config_name=self.config_name)
            self.control_device = Device(config)
            return self.control_device

    @staticmethod
    def escape_input_text(text):
        text = str(text)
        return (
            text
            .replace('%', r'\%')
            .replace(' ', '%s')
            .replace('&', r'\&')
            .replace('<', r'\<')
            .replace('>', r'\>')
            .replace('|', r'\|')
            .replace(';', r'\;')
            .replace('(', r'\(')
            .replace(')', r'\)')
        )

    def handle_local_control(self, payload):
        device = self.get_control_device()
        return handle_control_payload(device, payload)

    def on_mouse(self, event, x, y, flags, userdata=None):
        x, y = self.normalize_preview_point(x, y)

        if event == cv2.EVENT_LBUTTONDOWN:
            self.mouse_down = (x, y, time.time())
            self.realtime_touch_active = self.send_control({'cmd': 'touch_down', 'x': x, 'y': y})
            self.last_mouse_move_time = 0.0
        elif event == cv2.EVENT_MOUSEMOVE and self.realtime_touch_active and (flags & cv2.EVENT_FLAG_LBUTTON):
            now = time.time()
            if now - self.last_mouse_move_time >= PREVIEW_MOVE_INTERVAL:
                self.send_control({'cmd': 'touch_move', 'x': x, 'y': y})
                self.last_mouse_move_time = now
        elif event == cv2.EVENT_LBUTTONUP and self.mouse_down is not None:
            start_x, start_y, start_time = self.mouse_down
            self.mouse_down = None
            duration = max(0.05, time.time() - start_time)
            distance = ((x - start_x) ** 2 + (y - start_y) ** 2) ** 0.5
            if self.realtime_touch_active:
                self.send_control({'cmd': 'touch_move', 'x': x, 'y': y})
                self.send_control({'cmd': 'touch_up', 'x': x, 'y': y})
                self.realtime_touch_active = False
            elif distance <= PREVIEW_TAP_DISTANCE:
                self.send_control({'cmd': 'tap', 'x': x, 'y': y})
            else:
                self.send_control({
                    'cmd': 'swipe',
                    'x1': start_x,
                    'y1': start_y,
                    'x2': x,
                    'y2': y,
                    'duration': duration,
                })
        elif event == cv2.EVENT_RBUTTONUP:
            self.send_control({'cmd': 'keyevent', 'keycode': ANDROID_KEY_BACK})

    def normalize_preview_point(self, x, y):
        width, height = DEFAULT_OUTPUT_SIZE
        left, top, display_width, display_height = self.preview_content_rect
        if display_width > 0 and display_height > 0:
            x = (x - left) * width / display_width
            y = (y - top) * height / display_height
        return max(0, min(width - 1, int(x))), max(0, min(height - 1, int(y)))

    def on_key(self, key):
        key_map = {
            8: ANDROID_KEY_DEL,
            9: ANDROID_KEY_TAB,
            10: ANDROID_KEY_ENTER,
            13: ANDROID_KEY_ENTER,
            32: 62,
            2424832: ANDROID_KEY_DPAD_LEFT,
            2490368: ANDROID_KEY_DPAD_UP,
            2555904: ANDROID_KEY_DPAD_RIGHT,
            2621440: ANDROID_KEY_DPAD_DOWN,
        }
        if key in key_map:
            self.send_control({'cmd': 'keyevent', 'keycode': key_map[key]})
            return

        if 32 <= key <= 126:
            self.send_control({'cmd': 'text', 'text': chr(key)})

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
                    self.capture_frame = np.ascontiguousarray(frame)
                    self.raw_frame = None
                    self.raw_seq = 0
                    self.corrected_frame = None
                    self.corrected_seq = 0
                    self.frame = None
                    self.frame_time = now
                    self.seq += 1
                    self.frame_event.set()
                    if (
                        self.precorrect_interval is not None
                        and now - self.recent_frame_request_time <= 30
                        and now - self.last_precorrect_time >= self.precorrect_interval
                    ):
                        self.last_precorrect_time = now
                        self.correction_event.set()

                self.show_preview(frame, rgb=False)
        finally:
            if cap is not None:
                cap.release()
            if self.preview_window_created:
                try:
                    cv2.destroyWindow(SERVICE_WINDOW_NAME)
                except Exception:
                    pass

    def get_latest_frame(self, raw=False, profile=None):
        request_start = time.perf_counter()
        if not raw:
            self.recent_frame_request_time = time.time()
        deadline = time.time() + FRAME_TIMEOUT
        while time.time() < deadline:
            capture_frame = None
            frame_time = 0.0
            seq = 0
            with self.lock:
                if self.capture_frame is not None and time.time() - self.frame_time <= FRAME_TIMEOUT:
                    frame_time = self.frame_time
                    seq = self.seq
                    if not raw and self.corrected_frame is not None and self.corrected_seq == seq:
                        if profile is not None:
                            now = time.perf_counter()
                            profile['select_ms'] = (now - request_start) * 1000
                            profile['cache_hit'] = True
                            profile['frame_age_at_select_ms'] = (time.time() - frame_time) * 1000
                        return self.corrected_frame, frame_time, seq
                    if raw and self.raw_frame is not None and self.raw_seq == seq:
                        if profile is not None:
                            now = time.perf_counter()
                            profile['select_ms'] = (now - request_start) * 1000
                            profile['cache_hit'] = True
                            profile['frame_age_at_select_ms'] = (time.time() - frame_time) * 1000
                        return self.raw_frame, frame_time, seq
                    capture_frame = self.capture_frame

            if capture_frame is not None and raw:
                convert_start = time.perf_counter()
                raw_frame = self.convert_frame(capture_frame)
                if profile is not None:
                    profile['select_ms'] = (convert_start - request_start) * 1000
                    profile['cache_hit'] = False
                    profile['frame_age_at_select_ms'] = (time.time() - frame_time) * 1000
                    profile['normalize_ms'] = (time.perf_counter() - convert_start) * 1000
                    profile['color_correct_ms'] = 0.0
                with self.lock:
                    if self.seq == seq:
                        self.raw_frame = raw_frame
                        self.raw_seq = seq
                return raw_frame, frame_time, seq

            if capture_frame is not None:
                if profile is not None:
                    profile['select_ms'] = (time.perf_counter() - request_start) * 1000
                    profile['cache_hit'] = False
                    profile['frame_age_at_select_ms'] = (time.time() - frame_time) * 1000
                corrected = self.correct_capture_frame(capture_frame, profile=profile)
                with self.lock:
                    if self.seq == seq:
                        self.corrected_frame = corrected
                        self.corrected_seq = seq
                return corrected, frame_time, seq

            self.frame_event.wait(timeout=0.2)
            self.frame_event.clear()
        raise TimeoutError('Wait USB capture service frame timeout')

    def correction_loop(self):
        while not self.stop_event.is_set():
            self.correction_event.wait(timeout=0.5)
            self.correction_event.clear()
            if self.stop_event.is_set():
                break
            if self.precorrect_interval is None:
                continue
            if time.time() - self.recent_frame_request_time > 30:
                continue

            while not self.stop_event.is_set():
                with self.lock:
                    if self.capture_frame is None:
                        break
                    if self.corrected_frame is not None and self.corrected_seq == self.seq:
                        break
                    capture_frame = self.capture_frame
                    seq = self.seq
                    frame_time = self.frame_time

                corrected = self.correct_capture_frame(capture_frame)

                with self.lock:
                    if self.seq == seq:
                        self.corrected_frame = corrected
                        self.corrected_seq = seq

                # If capture advanced while correcting, loop once and skip the
                # stale result; otherwise wait for the next frame/request.
                with self.lock:
                    if self.seq == seq or self.frame_time == frame_time:
                        break

    def run(self):
        handler = self.make_handler()
        self.server = socketserver.ThreadingTCPServer((HOST, service_port(self.config_name)), handler)
        self.server.daemon_threads = True
        thread_server = threading.Thread(target=self.server.serve_forever, name='UsbCaptureServiceServer', daemon=True)
        thread_capture = threading.Thread(target=self.capture_loop, name='UsbCaptureServiceCapture', daemon=True)
        thread_correction = threading.Thread(target=self.correction_loop, name='UsbCaptureServiceCorrection', daemon=True)
        thread_server.start()
        thread_capture.start()
        thread_correction.start()
        print(f'USB capture service started: {HOST}:{service_port(self.config_name)}', flush=True)
        if self.preview_enabled:
            self.preload_control_device()
        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
            self.correction_event.set()
            thread_capture.join(timeout=2)
            thread_correction.join(timeout=2)
            self.shared_memory_close()
            print('USB capture service stopped', flush=True)

    def make_handler(self):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while not service.stop_event.is_set():
                    line = self.rfile.readline()
                    if not line:
                        break
                    try:
                        payload = json.loads(line.decode('utf-8'))
                    except Exception as e:
                        try:
                            send_json(self.request, {'ok': False, 'error': str(e)})
                        except Exception:
                            pass
                        break
                    cmd = payload.get('cmd')
                    try:
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
                            if service.preview_enabled:
                                service.preload_control_device()
                            send_json(self.request, {'ok': True, 'preview': service.preview_enabled})
                        elif cmd == 'stop':
                            service.stop_event.set()
                            send_json(self.request, {'ok': True})
                            break
                        elif cmd in ('frame', 'frame_raw', 'frame_shm', 'frame_shm_raw'):
                            request_perf = time.perf_counter()
                            request_wall = time.time()
                            raw = cmd in ('frame_raw', 'frame_shm_raw')
                            use_shm = cmd in ('frame_shm', 'frame_shm_raw')
                            profile = {} if payload.get('profile') else None
                            frame, frame_time, seq = service.get_latest_frame(raw=raw, profile=profile)
                            response = {
                                'ok': True,
                                'width': frame.shape[1],
                                'height': frame.shape[0],
                                'channels': frame.shape[2],
                                'size': int(frame.nbytes),
                                'time': frame_time,
                                'seq': seq,
                            }
                            if profile is not None:
                                profile['request_to_frame_capture_ms'] = (request_wall - frame_time) * 1000
                                profile['handler_before_reply_ms'] = (time.perf_counter() - request_perf) * 1000
                            if use_shm:
                                shm_name, shm_seq = service.shared_memory_frame(frame, profile=profile)
                                response['shm_name'] = shm_name
                                response['shm_seq'] = shm_seq
                                if profile is not None:
                                    profile['handler_before_reply_ms'] = (time.perf_counter() - request_perf) * 1000
                                    response['profile'] = profile
                                send_json(self.request, response)
                            else:
                                data = frame.tobytes()
                                if profile is not None:
                                    profile['socket_tobytes_ms'] = (
                                        (time.perf_counter() - request_perf) * 1000
                                        - profile.get('handler_before_reply_ms', 0.0)
                                    )
                                    profile['handler_before_reply_ms'] = (time.perf_counter() - request_perf) * 1000
                                    response['profile'] = profile
                                send_json(self.request, response)
                                self.request.sendall(data)
                        else:
                            send_json(self.request, {'ok': False, 'error': f'Unknown command: {cmd}'})
                    except Exception as e:
                        try:
                            send_json(self.request, {'ok': False, 'error': str(e)})
                        except Exception:
                            pass
                        break

        return Handler


def escape_input_text(text):
    text = str(text)
    return (
        text
        .replace('%', r'\%')
        .replace(' ', '%s')
        .replace('&', r'\&')
        .replace('<', r'\<')
        .replace('>', r'\>')
        .replace('|', r'\|')
        .replace(';', r'\;')
        .replace('(', r'\(')
        .replace(')', r'\)')
    )


def handle_control_payload(device, payload):
    cmd = payload.get('cmd')
    if cmd in ('touch_down', 'touch_move', 'touch_up'):
        x = int(payload.get('x', 0))
        y = int(payload.get('y', 0))
        realtime_touch(device, cmd, x, y)
        return {'ok': True}
    if cmd == 'tap':
        x = int(payload.get('x', 0))
        y = int(payload.get('y', 0))
        method = device.click_methods.get(device.config.Emulator_ControlMethod, device.click_adb)
        method(x, y)
        return {'ok': True}
    if cmd == 'swipe':
        p1 = (int(payload.get('x1', 0)), int(payload.get('y1', 0)))
        p2 = (int(payload.get('x2', 0)), int(payload.get('y2', 0)))
        duration = max(0.05, min(2.0, float(payload.get('duration', 0.1))))
        method = device.config.Emulator_ControlMethod
        if method == 'minitouch':
            device.swipe_minitouch(p1, p2)
        elif method == 'uiautomator2':
            device.swipe_uiautomator2(p1, p2, duration=duration)
        elif method == 'scrcpy':
            device.swipe_scrcpy(p1, p2)
        elif method == 'MaaTouch':
            device.swipe_maatouch(p1, p2)
        elif method == 'nemu_ipc':
            device.swipe_nemu_ipc(p1, p2)
        else:
            device.swipe_adb(p1, p2, duration=duration)
        return {'ok': True}
    if cmd == 'keyevent':
        device.adb_shell(['input', 'keyevent', int(payload.get('keycode', 0))])
        return {'ok': True}
    if cmd == 'text':
        text = str(payload.get('text', ''))
        if text:
            device.adb_shell(['input', 'text', escape_input_text(text)])
        return {'ok': True}
    return {'ok': False, 'error': f'Unknown command: {cmd}'}


def realtime_touch(device, cmd, x, y):
    method = device.config.Emulator_ControlMethod
    if method == 'MaaTouch':
        builder = device.maatouch_builder
        if cmd == 'touch_down':
            builder.down(x, y).commit()
        elif cmd == 'touch_move':
            builder.move(x, y).commit()
        else:
            builder.up().commit()
        send_maatouch_fast(device, builder)
        return
    if method == 'minitouch':
        builder = device.minitouch_builder
        if cmd == 'touch_down':
            builder.down(x, y).commit()
        elif cmd == 'touch_move':
            builder.move(x, y).commit()
        else:
            builder.up().commit()
        send_minitouch_fast(device, builder)
        return
    if method == 'scrcpy':
        from module.device.method.scrcpy import const
        device.scrcpy_ensure_running()
        action = {
            'touch_down': const.ACTION_DOWN,
            'touch_move': const.ACTION_MOVE,
            'touch_up': const.ACTION_UP,
        }[cmd]
        with device._scrcpy_control_socket_lock:
            device._scrcpy_control.touch(x, y, action)
        return
    if method == 'nemu_ipc':
        if cmd in ('touch_down', 'touch_move'):
            device.nemu_ipc.down(x, y)
        else:
            device.nemu_ipc.up()
        return
    raise RuntimeError(f'Realtime touch is not supported by ControlMethod {method}')


def send_maatouch_fast(device, builder):
    content = builder.to_minitouch()
    device._maatouch_stream.sendall(content.encode('utf-8'))
    device._maatouch_stream.recv(0)
    builder.clear()


def send_minitouch_fast(device, builder):
    if device.config.DEVICE_OVER_HTTP:
        content = builder.to_atx_agent()

        async def send():
            for row in content:
                await device._minitouch_ws.send(row)

        device._minitouch_loop_run(send())
    else:
        content = builder.to_minitouch()
        device._minitouch_client.sendall(content.encode('utf-8'))
        device._minitouch_client.recv(0)
    builder.clear()


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
