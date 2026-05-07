import time
import threading
import queue

import cv2
import numpy as np

from module.base.decorator import cached_property, del_cached_property, has_cached_property
from module.device.env import IS_MACINTOSH, IS_WINDOWS
from module.exception import RequestHumanTakeover
from module.logger import logger


class UsbCaptureError(Exception):
    pass


class UsbCapture:
    _usb_capture_last_frame_time = 0.0
    _USB_CAPTURE_OPEN_TIMEOUT = 5
    _USB_CAPTURE_READ_TIMEOUT = 2
    _USB_CAPTURE_FRAME_TIMEOUT = 5
    _USB_CAPTURE_OUTPUT_SIZE = (1280, 720)
    _USB_CAPTURE_PREVIEW_WINDOW = 'Alas USB Capture Preview'

    @staticmethod
    def _usb_capture_backend(backend):
        backend = str(backend).strip().lower()
        if backend == 'auto':
            if IS_WINDOWS:
                return getattr(cv2, 'CAP_DSHOW', cv2.CAP_ANY)
            if IS_MACINTOSH:
                return getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY)
            return getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY)

        mapping = {
            'any': cv2.CAP_ANY,
            'dshow': getattr(cv2, 'CAP_DSHOW', cv2.CAP_ANY),
            'msmf': getattr(cv2, 'CAP_MSMF', cv2.CAP_ANY),
            'v4l2': getattr(cv2, 'CAP_V4L2', cv2.CAP_ANY),
            'avfoundation': getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY),
        }
        return mapping.get(backend, cv2.CAP_ANY)

    @staticmethod
    def _usb_capture_device(device):
        device = str(device).strip()
        try:
            return int(device)
        except ValueError:
            return device

    @staticmethod
    def _usb_capture_fourcc(codec):
        codec = str(codec).strip().upper()
        if codec in ('', 'AUTO', 'DEFAULT'):
            return None
        if codec == 'MJPEG':
            codec = 'MJPG'
        if len(codec) != 4:
            logger.warning(f'Invalid USB capture codec: {codec}, use driver default')
            return None
        return cv2.VideoWriter_fourcc(*codec)

    @staticmethod
    def _usb_capture_fourcc_name(value):
        try:
            value = int(value)
            chars = ''.join(chr((value >> 8 * i) & 0xFF) for i in range(4))
            if chars.strip('\x00 '):
                return chars
        except Exception:
            pass
        return 'unknown'

    @staticmethod
    def _usb_capture_call_timeout(func, timeout, error):
        result = queue.Queue(maxsize=1)

        def run():
            try:
                result.put((True, func()), block=False)
            except Exception as e:
                result.put((False, e), block=False)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            ok, value = result.get(timeout=timeout)
        except queue.Empty:
            raise UsbCaptureError(error)
        if ok:
            return value
        raise value

    @staticmethod
    def _usb_capture_resize(image, width, height):
        current_height, current_width = image.shape[:2]
        if current_width == width and current_height == height:
            return image

        # Many USB HDMI capture cards expose a fallback 640x480 stream
        # containing a squeezed 16:9 image. Stretch it back instead of
        # center-cropping, otherwise UI text remains horizontally compressed.
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

    def _usb_capture_convert_frame(self, frame):
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        width, height = self._USB_CAPTURE_OUTPUT_SIZE
        return np.ascontiguousarray(self._usb_capture_resize(frame, width, height))

    def _usb_capture_frame_objects(self):
        if not hasattr(self, '_usb_capture_frame_lock'):
            self._usb_capture_frame_lock = threading.Lock()
            self._usb_capture_frame_event = threading.Event()
            self._usb_capture_latest_frame = None
            self._usb_capture_stream_error = None
            self._usb_capture_stream_seq = 0
        return self._usb_capture_frame_lock, self._usb_capture_frame_event

    @cached_property
    def usb_capture(self):
        device = self._usb_capture_device(self.config.Emulator_UsbCaptureDevice)
        backend = self._usb_capture_backend(self.config.Emulator_UsbCaptureBackend)
        width = int(self.config.Emulator_UsbCaptureWidth)
        height = int(self.config.Emulator_UsbCaptureHeight)
        fps = int(self.config.Emulator_UsbCaptureFps)
        fourcc = self._usb_capture_fourcc(getattr(self.config, 'Emulator_UsbCaptureCodec', 'MJPG'))

        def open_capture():
            logger.info(f'Opening USB capture device: {device}, backend={self.config.Emulator_UsbCaptureBackend}')
            try:
                opened = self._usb_capture_call_timeout(
                    lambda: cv2.VideoCapture(device, backend),
                    timeout=self._USB_CAPTURE_OPEN_TIMEOUT,
                    error=f'Open USB capture device timeout: {device}'
                )
            except UsbCaptureError as e:
                logger.critical(str(e))
                logger.critical('Try setting Emulator.UsbCaptureBackend to dshow or msmf')
                raise RequestHumanTakeover
            if not opened.isOpened():
                logger.critical(f'Unable to open USB capture device: {device}')
                raise RequestHumanTakeover
            return opened

        cap = open_capture()

        def configure():
            if fourcc is not None:
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, fps)
            actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            actual_fps = cap.get(cv2.CAP_PROP_FPS) or 0
            actual_fourcc = self._usb_capture_fourcc_name(cap.get(cv2.CAP_PROP_FOURCC) or 0)
            return actual_width, actual_height, actual_fps, actual_fourcc

        try:
            actual_width, actual_height, actual_fps, actual_fourcc = self._usb_capture_call_timeout(
                configure,
                timeout=self._USB_CAPTURE_OPEN_TIMEOUT,
                error=f'Configure USB capture device timeout: {device}'
            )
        except UsbCaptureError as e:
            logger.critical(str(e))
            logger.critical('Try setting Emulator.UsbCaptureBackend to dshow or msmf')
            try:
                cap.release()
            except Exception:
                pass
            raise RequestHumanTakeover
        logger.attr('UsbCapture', f'{actual_fourcc} {actual_width}x{actual_height} @ {actual_fps:.2f}fps')

        try:
            ok, frame = self._usb_capture_call_timeout(
                cap.read,
                timeout=self._USB_CAPTURE_READ_TIMEOUT,
                error='Read frame from configured USB capture device timeout'
            )
        except UsbCaptureError:
            ok, frame = False, None
        if not (ok and frame is not None and frame.size):
            logger.warning('Configured USB capture mode produced no frame, retry with driver default mode')
            try:
                cap.release()
            except Exception:
                pass
            cap = open_capture()
            try:
                ok, frame = self._usb_capture_call_timeout(
                    cap.read,
                    timeout=self._USB_CAPTURE_READ_TIMEOUT,
                    error='Read frame from default USB capture device timeout'
                )
            except UsbCaptureError:
                ok, frame = False, None
            if ok and frame is not None and frame.size:
                actual_height, actual_width = frame.shape[:2]
                actual_fps = cap.get(cv2.CAP_PROP_FPS) or 0
                actual_fourcc = self._usb_capture_fourcc_name(cap.get(cv2.CAP_PROP_FOURCC) or 0)
                logger.attr('UsbCapture', f'{actual_fourcc} {actual_width}x{actual_height} @ {actual_fps:.2f}fps (default)')
            else:
                try:
                    cap.release()
                except Exception:
                    pass
                logger.critical('Unable to read frame from USB capture device')
                raise RequestHumanTakeover
        return cap

    def usb_capture_release(self):
        self.usb_capture_stream_stop()
        if has_cached_property(self, 'usb_capture'):
            try:
                self.usb_capture.release()
            except Exception as e:
                logger.warning(f'Failed to release USB capture device: {e}')
            del_cached_property(self, 'usb_capture')
        self.usb_capture_preview_stop()

    def usb_capture_stream_stop(self):
        stop_event = getattr(self, '_usb_capture_stream_stop_event', None)
        thread = getattr(self, '_usb_capture_stream_thread', None)
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._usb_capture_stream_thread = None
        self._usb_capture_stream_stop_event = None

    def usb_capture_stream_worker(self, stop_event):
        lock, frame_event = self._usb_capture_frame_objects()
        failed_reads = 0
        last_warning = 0

        try:
            cap = self.usb_capture
            while not stop_event.is_set():
                ok, frame = cap.read()
                if not (ok and frame is not None and frame.size):
                    failed_reads += 1
                    now = time.time()
                    if now - last_warning > 5:
                        logger.warning(f'Unable to read frame from USB capture device, failed={failed_reads}')
                        last_warning = now
                    time.sleep(0.05)
                    continue

                failed_reads = 0
                try:
                    frame = self._usb_capture_convert_frame(frame)
                except Exception as e:
                    logger.warning(f'Invalid USB capture frame: {e}')
                    continue

                with lock:
                    self._usb_capture_latest_frame = frame
                    self._usb_capture_last_frame_time = time.time()
                    self._usb_capture_stream_seq += 1
                    self._usb_capture_stream_error = None
                    frame_event.set()

                self.usb_capture_preview(frame)
        except Exception as e:
            with lock:
                self._usb_capture_stream_error = e
                frame_event.set()
            logger.warning(f'USB capture stream stopped: {e}')

    def usb_capture_stream_start(self):
        thread = getattr(self, '_usb_capture_stream_thread', None)
        if thread is not None and thread.is_alive():
            return

        self._usb_capture_frame_objects()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.usb_capture_stream_worker,
            args=(stop_event,),
            name='UsbCaptureStream',
            daemon=True
        )
        self._usb_capture_stream_stop_event = stop_event
        self._usb_capture_stream_thread = thread
        thread.start()

    def usb_capture_latest_frame(self):
        self.usb_capture_stream_start()
        lock, frame_event = self._usb_capture_frame_objects()

        deadline = time.time() + self._USB_CAPTURE_FRAME_TIMEOUT
        while time.time() < deadline:
            with lock:
                error = self._usb_capture_stream_error
                frame = self._usb_capture_latest_frame
                frame_time = self._usb_capture_last_frame_time
                if error is not None:
                    raise error
                if frame is not None and time.time() - frame_time <= self._USB_CAPTURE_FRAME_TIMEOUT:
                    return frame.copy()

            timeout = max(0.05, min(0.5, deadline - time.time()))
            frame_event.wait(timeout=timeout)
            frame_event.clear()

        raise UsbCaptureError('Wait USB capture frame timeout')

    def usb_capture_preview_stop(self):
        stop_event = getattr(self, '_usb_capture_preview_stop_event', None)
        frame_queue = getattr(self, '_usb_capture_preview_queue', None)
        thread = getattr(self, '_usb_capture_preview_thread', None)
        if stop_event is not None:
            stop_event.set()
        if frame_queue is not None:
            try:
                frame_queue.put_nowait(None)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._usb_capture_preview_thread = None
        self._usb_capture_preview_queue = None
        self._usb_capture_preview_stop_event = None

    def usb_capture_preview_worker(self, frame_queue, stop_event):
        window_created = False
        try:
            while not stop_event.is_set():
                try:
                    image = frame_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if image is None:
                    break

                try:
                    if not window_created:
                        cv2.namedWindow(self._USB_CAPTURE_PREVIEW_WINDOW, cv2.WINDOW_NORMAL)
                        cv2.resizeWindow(self._USB_CAPTURE_PREVIEW_WINDOW, 960, 540)
                        window_created = True

                    cv2.imshow(self._USB_CAPTURE_PREVIEW_WINDOW, cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
                    key = cv2.waitKey(1) & 0xFF
                except Exception as e:
                    logger.warning(f'USB capture preview disabled: {e}')
                    self.config.Emulator_UsbCapturePreview = False
                    break

                if key in (27, ord('q')):
                    logger.info('USB capture preview closed')
                    self.config.Emulator_UsbCapturePreview = False
                    break
        finally:
            if window_created:
                try:
                    cv2.destroyWindow(self._USB_CAPTURE_PREVIEW_WINDOW)
                except Exception:
                    pass

    def usb_capture_preview_start(self):
        thread = getattr(self, '_usb_capture_preview_thread', None)
        if thread is not None and thread.is_alive():
            return

        frame_queue = queue.Queue(maxsize=1)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.usb_capture_preview_worker,
            args=(frame_queue, stop_event),
            name='UsbCapturePreview',
            daemon=True
        )
        self._usb_capture_preview_queue = frame_queue
        self._usb_capture_preview_stop_event = stop_event
        self._usb_capture_preview_thread = thread
        thread.start()

    def usb_capture_preview(self, image):
        if not bool(getattr(self.config, 'Emulator_UsbCapturePreview', False)):
            self.usb_capture_preview_stop()
            return

        self.usb_capture_preview_start()
        frame_queue = getattr(self, '_usb_capture_preview_queue', None)
        if frame_queue is None:
            return
        try:
            frame_queue.put_nowait(image.copy())
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                frame_queue.put_nowait(image.copy())
            except queue.Full:
                pass

    def screenshot_usb_capture(self):
        try:
            return self.usb_capture_latest_frame()
        except Exception as e:
            logger.critical(str(e))
            self.usb_capture_release()
            raise RequestHumanTakeover
