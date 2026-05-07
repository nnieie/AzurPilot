import time

import cv2
import numpy as np

from module.base.decorator import cached_property, del_cached_property, has_cached_property
from module.exception import RequestHumanTakeover
from module.logger import logger


class UsbCaptureError(Exception):
    pass


class UsbCapture:
    _usb_capture_last_frame_time = 0.0

    @staticmethod
    def _usb_capture_backend(backend):
        backend = str(backend).strip().lower()
        mapping = {
            'auto': cv2.CAP_ANY,
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
    def _usb_capture_resize(image, width, height):
        current_height, current_width = image.shape[:2]
        if current_width == width and current_height == height:
            return image

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

    @cached_property
    def usb_capture(self):
        device = self._usb_capture_device(self.config.Emulator_UsbCaptureDevice)
        backend = self._usb_capture_backend(self.config.Emulator_UsbCaptureBackend)
        width = int(self.config.Emulator_UsbCaptureWidth)
        height = int(self.config.Emulator_UsbCaptureHeight)
        fps = int(self.config.Emulator_UsbCaptureFps)

        logger.info(f'Opening USB capture device: {device}, backend={self.config.Emulator_UsbCaptureBackend}')
        cap = cv2.VideoCapture(device, backend)
        if not cap.isOpened():
            logger.critical(f'Unable to open USB capture device: {device}')
            raise RequestHumanTakeover

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))

        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        logger.attr('UsbCapture', f'{actual_width}x{actual_height} @ {actual_fps:.2f}fps')
        return cap

    def usb_capture_release(self):
        if has_cached_property(self, 'usb_capture'):
            try:
                self.usb_capture.release()
            except Exception as e:
                logger.warning(f'Failed to release USB capture device: {e}')
            del_cached_property(self, 'usb_capture')

    def screenshot_usb_capture(self):
        width = int(self.config.Emulator_UsbCaptureWidth)
        height = int(self.config.Emulator_UsbCaptureHeight)

        last_error = None
        for _ in range(3):
            ok, frame = self.usb_capture.read()
            if ok and frame is not None and frame.size:
                break
            last_error = UsbCaptureError('Unable to read frame from USB capture device')
            time.sleep(0.05)
        else:
            logger.critical(str(last_error))
            self.usb_capture_release()
            raise RequestHumanTakeover

        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        else:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        frame = self._usb_capture_resize(frame, width, height)
        self._usb_capture_last_frame_time = time.time()
        return np.ascontiguousarray(frame)
