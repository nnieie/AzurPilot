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
CONTROL_BASE_PORT = 28180
PORT_RANGE = 1000
FRAME_TIMEOUT = 5.0
SERVICE_WINDOW_NAME = 'Alas USB Capture Preview'
PREVIEW_TAP_DISTANCE = 10
PREVIEW_MOVE_INTERVAL = 0.016

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
        self.mouse_down = None
        self.last_control_warning = 0.0
        self.control_lock = threading.Lock()
        self.control_device = None
        self.control_preload_started = False
        self.realtime_touch_active = False
        self.last_mouse_move_time = 0.0
        self.remote_control_retry_time = 0.0

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
            cv2.resizeWindow(SERVICE_WINDOW_NAME, 960, 540)
            cv2.setMouseCallback(SERVICE_WINDOW_NAME, self.on_mouse)
            self.preview_window_created = True

        preview = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        cv2.imshow(SERVICE_WINDOW_NAME, preview)
        key = cv2.waitKeyEx(1)
        try:
            visible = cv2.getWindowProperty(SERVICE_WINDOW_NAME, cv2.WND_PROP_VISIBLE) >= 1
        except Exception:
            visible = False
        if key in (27, ord('q'), ord('Q')) or not visible:
            self.preview_enabled = False
            try:
                cv2.destroyWindow(SERVICE_WINDOW_NAME)
            except Exception:
                pass
            self.preview_window_created = False
        elif key != -1:
            self.on_key(key)

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
        if self.preview_enabled:
            self.preload_control_device()
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
                        if service.preview_enabled:
                            service.preload_control_device()
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
