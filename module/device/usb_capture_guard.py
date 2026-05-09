import glob
import json
import os
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

from module.config.utils import filepath_config
from module.device.usb_capture_guard_stats import (
    record_usb_capture_guard_stat,
    safe_name,
    write_usb_capture_guard_event,
)
from module.logger import logger


INVALID_PATH_CHARS = set('<>:"/\\|?*')
DEFAULT_OPTIONS = {
    'enabled': False,
    'auto_calibration': False,
    'consecutive_failures': 2,
    'adb_cache_seconds': 0.75,
    'adb_min_interval': 1.0,
    'sample_retention': 200,
    'auto_calibration_min_interval': 120.0,
}


def _safe_name(value, fallback='unknown'):
    return safe_name(value, fallback=fallback)


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _save_rgb(path, image):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, cv2.cvtColor(image, cv2.COLOR_RGB2BGR))


def _resize_like(image, reference):
    if image is None or reference is None:
        return image
    if image.shape[:2] == reference.shape[:2]:
        return np.ascontiguousarray(image[:, :, :3])
    height, width = reference.shape[:2]
    return np.ascontiguousarray(cv2.resize(image[:, :, :3], (width, height), interpolation=cv2.INTER_AREA))


class UsbCaptureGuard:
    def __init__(self, main):
        self.main = main
        self.config = main.config
        self.device = main.device
        self.options = self._load_options()
        self.failures = {}
        self.adb_image = None
        self.adb_time = 0.0
        self.last_adb_attempt = 0.0
        self.last_options_check = 0.0
        self.config_mtime = None
        self.guard_mtime = None
        self.retrain_process = None
        self.retrain_log = None
        self.last_retrain_start = 0.0
        self.warned = set()

    @property
    def config_name(self):
        return getattr(self.config, 'config_name', 'alas')

    def _option_path(self):
        return os.path.join('config', 'usb_color', f'{self.config_name}.guard.json')

    @staticmethod
    def _deep_get(data, keys, default=None):
        current = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def _load_options(self):
        options = dict(DEFAULT_OPTIONS)
        path = self._option_path()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                if isinstance(data, dict):
                    options.update(data)
            except Exception as e:
                logger.warning(f'Failed to load USB capture guard options: {path}, {e}')
        config_path = filepath_config(self.config_name)
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                enabled = self._deep_get(data, ['Alas', 'Emulator', 'UsbCaptureAdbFallback'])
                auto_calibration = self._deep_get(data, ['Alas', 'Emulator', 'UsbCaptureAutoCalibration'])
                if enabled is not None:
                    options['enabled'] = bool(enabled)
                if auto_calibration is not None:
                    options['auto_calibration'] = bool(auto_calibration)
            except Exception as e:
                logger.warning(f'Failed to load USB capture guard config: {config_path}, {e}')
        return options

    def _refresh_options(self):
        now = time.time()
        if now - self.last_options_check < 2.0:
            return
        self.last_options_check = now

        config_path = filepath_config(self.config_name)
        guard_path = self._option_path()
        config_mtime = os.path.getmtime(config_path) if os.path.exists(config_path) else None
        guard_mtime = os.path.getmtime(guard_path) if os.path.exists(guard_path) else None
        if config_mtime == self.config_mtime and guard_mtime == self.guard_mtime:
            return

        self.config_mtime = config_mtime
        self.guard_mtime = guard_mtime
        self.options = self._load_options()

    def enabled(self):
        self._refresh_options()
        if not (self.options.get('enabled', False) or self.options.get('auto_calibration', False)):
            return False
        return getattr(self.config, 'Emulator_ScreenshotMethod', None) == 'usb_capture'

    def auto_calibration_enabled(self):
        self._refresh_options()
        if not self.options.get('auto_calibration', False):
            return False
        return getattr(self.config, 'Emulator_ScreenshotMethod', None) == 'usb_capture'

    def evaluate(self, button, appear, mode, offset=0, similarity=0.85, threshold=10):
        if appear:
            self.failures.pop(self._key(button, mode), None)
            self._poll_retrain_process()
            return True
        if not self.enabled():
            return appear

        key = self._key(button, mode)
        count = self.failures.get(key, 0) + 1
        self.failures[key] = count
        if count < int(self.options.get('consecutive_failures', 2)):
            return appear

        usb_image = getattr(self.device, 'image', None)
        adb = self._adb_screenshot(usb_image)
        if adb is None:
            return appear

        adb_result = self._evaluate_on_image(
            button,
            adb,
            mode=mode,
            offset=offset,
            similarity=similarity,
            threshold=threshold,
        )
        if not adb_result:
            write_usb_capture_guard_event(
                self.config_name,
                'adb_fallback_miss',
                'ADB screenshot also failed to match',
                target=getattr(button, 'name', str(button)),
                mode=mode,
                consecutive_failures=count,
                offset=offset,
                similarity=similarity,
                threshold=threshold,
                area=getattr(button, 'area', None),
                color=getattr(button, 'color', None),
                task=getattr(self.config, 'task', None),
            )
            return appear

        self.failures.pop(key, None)
        record_usb_capture_guard_stat(self.config_name, 'fallback')
        logger.warning(f'USB capture guard recovered {button} via ADB judge ({mode})')
        sample_info = self._save_sample(
            button,
            mode=mode,
            usb_image=usb_image,
            adb_image=adb,
            offset=offset,
            similarity=similarity,
            threshold=threshold,
        )
        write_usb_capture_guard_event(
            self.config_name,
            'adb_fallback',
            'Recovered button/template recognition with ADB screenshot',
            target=getattr(button, 'name', str(button)),
            mode=mode,
            consecutive_failures=count,
            offset=offset,
            similarity=similarity,
            threshold=threshold,
            area=getattr(button, 'area', None),
            color=getattr(button, 'color', None),
            task=getattr(self.config, 'task', None),
            sample=(sample_info or {}).get('path'),
            sample_saved=bool(sample_info),
            raw_available=bool((sample_info or {}).get('raw_available')),
            usb_shape=self._image_shape(usb_image),
            adb_shape=self._image_shape(adb),
        )
        if sample_info is not None and sample_info.get('raw_available'):
            self._maybe_start_retrain()
        return True

    def get_adb_image(self, usb_image=None):
        if not self.enabled():
            return None
        if usb_image is None:
            usb_image = getattr(self.device, 'image', None)
        return self._adb_screenshot(usb_image)

    def record_image_recovery(self, name, mode, usb_image, adb_image, metadata=None):
        record_usb_capture_guard_stat(self.config_name, 'fallback')
        sample_info = self._save_image_sample(
            name=name,
            mode=mode,
            usb_image=usb_image,
            adb_image=adb_image,
            metadata=metadata,
        )
        write_usb_capture_guard_event(
            self.config_name,
            'adb_fallback',
            'Recovered image recognition with ADB screenshot',
            target=name,
            mode=mode,
            task=getattr(self.config, 'task', None),
            sample=(sample_info or {}).get('path'),
            sample_saved=bool(sample_info),
            raw_available=bool((sample_info or {}).get('raw_available')),
            usb_shape=self._image_shape(usb_image),
            adb_shape=self._image_shape(adb_image),
            metadata=metadata,
        )
        logger.warning(f'USB capture guard recovered {name} via ADB judge ({mode})')
        if sample_info is not None and sample_info.get('raw_available'):
            self._maybe_start_retrain()

    def _key(self, button, mode):
        return f'{mode}:{getattr(button, "name", str(button))}'

    def _adb_screenshot(self, usb_image):
        now = time.time()
        cache_seconds = float(self.options.get('adb_cache_seconds', 0.75))
        if self.adb_image is not None and now - self.adb_time <= cache_seconds:
            return self.adb_image

        min_interval = float(self.options.get('adb_min_interval', 1.0))
        if now - self.last_adb_attempt < min_interval:
            return None

        self.last_adb_attempt = now
        try:
            image = self.device.screenshot_adb()
            image = _resize_like(image, usb_image)
        except Exception as e:
            marker = type(e).__name__
            if marker not in self.warned:
                self.warned.add(marker)
                logger.warning(f'USB capture guard ADB screenshot failed: {e}')
                write_usb_capture_guard_event(
                    self.config_name,
                    'adb_screenshot_failed',
                    'Failed to capture ADB screenshot for fallback',
                    error_type=type(e).__name__,
                    error=str(e),
                    task=getattr(self.config, 'task', None),
                )
            return None

        self.adb_image = image
        self.adb_time = time.time()
        return image

    @staticmethod
    def _evaluate_on_image(button, image, mode, offset=0, similarity=0.85, threshold=10):
        try:
            if mode == 'appear_on':
                return button.appear_on(image, threshold=threshold)
            if mode == 'match':
                return button.match(image, offset=offset, similarity=similarity)
            if mode == 'match_template_color':
                return button.match_template_color(
                    image,
                    offset=offset,
                    similarity=similarity,
                    threshold=threshold,
                )
        except Exception as e:
            logger.warning(f'USB capture guard evaluate failed for {button}: {e}')
        return False

    @staticmethod
    def _image_shape(image):
        if image is None:
            return None
        return list(getattr(image, 'shape', []))

    def _sample_root(self):
        return os.path.join('log', 'usb_capture_guard', _safe_name(self.config_name))

    def _capture_raw_usb(self):
        try:
            from dev_tools.usb_capture_service import get_frame

            return get_frame(self.config_name, timeout=1.0, raw=True, persistent=True)
        except Exception as e:
            marker = f'raw:{type(e).__name__}'
            if marker not in self.warned:
                self.warned.add(marker)
                logger.warning(f'USB capture guard raw USB sample unavailable: {e}')
            return None

    def _save_sample(self, button, mode, usb_image, adb_image, offset=0, similarity=0.85, threshold=10):
        button_name = _safe_name(getattr(button, 'name', str(button)))
        metadata = {
            'button': str(button),
            'button_name': getattr(button, 'name', str(button)),
            'area': _jsonable(getattr(button, 'area', None)),
            'color': _jsonable(getattr(button, 'color', None)),
            'button_area': _jsonable(getattr(button, 'button', None)),
            'offset': _jsonable(offset),
            'similarity': float(similarity),
            'threshold': float(threshold),
        }
        return self._save_image_sample(
            name=button_name,
            mode=mode,
            usb_image=usb_image,
            adb_image=adb_image,
            metadata=metadata,
        )

    def _save_image_sample(self, name, mode, usb_image, adb_image, metadata=None):
        if not self.auto_calibration_enabled():
            return None
        if usb_image is None or adb_image is None:
            return None

        timestamp = time.strftime('%Y%m%d_%H%M%S')
        millis = int((time.time() % 1) * 1000)
        sample_name = _safe_name(name)
        dirname = f'{timestamp}_{millis:03d}_{sample_name}_{_safe_name(mode)}'
        sample_dir = os.path.join(self._sample_root(), dirname)

        raw_image = self._capture_raw_usb()
        try:
            paths = {
                'usb': os.path.join(sample_dir, 'usb.png'),
                'adb': os.path.join(sample_dir, 'adb.png'),
            }
            _save_rgb(paths['usb'], usb_image)
            _save_rgb(paths['adb'], adb_image)
            if raw_image is not None:
                paths['usb_raw'] = os.path.join(sample_dir, 'usb_raw.png')
                _save_rgb(paths['usb_raw'], raw_image)

            data = {
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'config_name': self.config_name,
                'task': getattr(self.config, 'task', None),
                'screenshot_method': getattr(self.config, 'Emulator_ScreenshotMethod', None),
                'name': name,
                'mode': mode,
                'usb_result': False,
                'adb_result': True,
                'images': {key: path.replace('\\', '/') for key, path in paths.items()},
                'notes': {
                    'usb': 'current Alas USB image, possibly color corrected',
                    'usb_raw': 'raw USB service frame before color correction' if raw_image is not None else None,
                    'adb': 'ADB screencap reference',
                },
            }
            if metadata:
                data.update(_jsonable(metadata))

            with open(os.path.join(sample_dir, 'metadata.json'), 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
            self._prune_samples()
            if raw_image is not None:
                record_usb_capture_guard_stat(self.config_name, 'auto_calibration')
                write_usb_capture_guard_event(
                    self.config_name,
                    'auto_calibration_sample',
                    'Saved usable failure sample for USB color calibration',
                    target=name,
                    mode=mode,
                    sample=sample_dir.replace('\\', '/'),
                    metadata=metadata,
                    raw_available=True,
                )
            else:
                write_usb_capture_guard_event(
                    self.config_name,
                    'auto_calibration_sample_no_raw',
                    'Saved fallback sample, but raw USB frame was unavailable for auto calibration',
                    target=name,
                    mode=mode,
                    sample=sample_dir.replace('\\', '/'),
                    metadata=metadata,
                    raw_available=False,
                )
            return {
                'path': sample_dir.replace('\\', '/'),
                'raw_available': raw_image is not None,
            }
        except Exception as e:
            logger.warning(f'USB capture guard failed to save sample: {e}')
            write_usb_capture_guard_event(
                self.config_name,
                'sample_save_failed',
                'Failed to save USB capture guard sample',
                target=name,
                mode=mode,
                error_type=type(e).__name__,
                error=str(e),
            )
            return None

    def _prune_samples(self):
        keep = int(self.options.get('sample_retention', 200))
        if keep <= 0:
            return
        dirs = [path for path in glob.glob(os.path.join(self._sample_root(), '*')) if os.path.isdir(path)]
        if len(dirs) <= keep:
            return
        dirs.sort(key=lambda path: os.path.getmtime(path))
        for path in dirs[:len(dirs) - keep]:
            try:
                shutil.rmtree(path)
            except Exception as e:
                logger.warning(f'USB capture guard failed to prune sample {path}: {e}')

    def _poll_retrain_process(self):
        process = self.retrain_process
        if process is None:
            return
        code = process.poll()
        if code is None:
            return
        log_path = getattr(self, 'retrain_log_path', None)
        if self.retrain_log is not None:
            try:
                self.retrain_log.close()
            except Exception:
                pass
        self.retrain_process = None
        self.retrain_log = None
        if code == 0:
            write_usb_capture_guard_event(
                self.config_name,
                'auto_calibration_finished',
                'Auto calibration process finished',
                code=code,
                log=log_path,
            )
            logger.info(f'USB capture guard auto calibration finished, log={log_path}')
        else:
            write_usb_capture_guard_event(
                self.config_name,
                'auto_calibration_failed',
                'Auto calibration process failed',
                code=code,
                log=log_path,
            )
            logger.warning(f'USB capture guard auto calibration failed with code {code}, log={log_path}')

    def _maybe_start_retrain(self):
        self._poll_retrain_process()
        if self.retrain_process is not None:
            return
        now = time.time()
        min_interval = float(self.options.get('auto_calibration_min_interval', 120.0))
        if now - self.last_retrain_start < min_interval:
            return

        log_dir = self._sample_root()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f'retrain_{time.strftime("%Y%m%d_%H%M%S")}.log')
        cmd = [
            sys.executable,
            'dev_tools/usb_capture_guard_retrain.py',
            '--config-name',
            self.config_name,
            '--apply',
        ]
        creationflags = 0
        if sys.platform == 'win32':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        log_file = None
        try:
            log_file = open(log_path, 'a', encoding='utf-8')
            log_file.write(' '.join(cmd) + '\n')
            log_file.flush()
            self.retrain_process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')),
                creationflags=creationflags,
            )
            self.retrain_log = log_file
            self.retrain_log_path = log_path.replace('\\', '/')
            self.last_retrain_start = now
            write_usb_capture_guard_event(
                self.config_name,
                'auto_calibration_started',
                'Auto calibration process started',
                log=self.retrain_log_path,
                command=cmd,
                sample_root=log_dir.replace('\\', '/'),
            )
            logger.info(f'USB capture guard auto calibration started, log={self.retrain_log_path}')
        except Exception as e:
            logger.warning(f'USB capture guard failed to start auto calibration: {e}')
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass


def _guard(main):
    guard = getattr(main, '_usb_capture_guard', None)
    if guard is None:
        guard = UsbCaptureGuard(main)
        main._usb_capture_guard = guard
    return guard


def usb_capture_guard_appear(main, button, appear, mode, offset=0, similarity=0.85, threshold=10):
    return _guard(main).evaluate(
        button,
        appear,
        mode=mode,
        offset=offset,
        similarity=similarity,
        threshold=threshold,
    )


def usb_capture_guard_get_adb_image(main, usb_image=None):
    return _guard(main).get_adb_image(usb_image=usb_image)


def usb_capture_guard_record_image_recovery(main, name, mode, usb_image, adb_image, metadata=None):
    return _guard(main).record_image_recovery(
        name=name,
        mode=mode,
        usb_image=usb_image,
        adb_image=adb_image,
        metadata=metadata,
    )
