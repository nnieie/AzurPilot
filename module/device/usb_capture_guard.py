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
    'consecutive_failures': 1,
    'adb_cache_seconds': 0.75,
    'adb_min_interval': 1.0,
    'adb_miss_log_interval': 30.0,
    'sample_max_pair_delay': 1.5,
    'sample_min_edge_correlation': 0.20,
    'sample_min_luma_correlation': 0.30,
    'sample_max_normalized_luma_mae': 0.90,
    'sample_retention': 200,
    'auto_calibration_min_interval': 120.0,
}


def _safe_name(value, fallback='unknown'):
    return safe_name(value, fallback=fallback)


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        return _jsonable(value.tolist())
    except Exception:
        return str(value)


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
        self.adb_meta = None
        self.last_adb_attempt = 0.0
        self.miss_log_time = {}
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
        candidate = self._fallback_candidate(button, mode)
        if not candidate.get('eligible'):
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
            if self._should_log_miss(key, count):
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
                    usb_candidate=candidate,
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
            usb_candidate=candidate,
            task=getattr(self.config, 'task', None),
            sample=(sample_info or {}).get('path'),
            sample_saved=bool(sample_info),
            raw_available=bool((sample_info or {}).get('raw_available')),
            usable_for_calibration=bool((sample_info or {}).get('usable_for_calibration')),
            usb_shape=self._image_shape(usb_image),
            adb_shape=self._image_shape(adb),
        )
        if sample_info is not None and sample_info.get('usable_for_calibration'):
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
            usable_for_calibration=bool((sample_info or {}).get('usable_for_calibration')),
            usb_shape=self._image_shape(usb_image),
            adb_shape=self._image_shape(adb_image),
            metadata=metadata,
        )
        logger.warning(f'USB capture guard recovered {name} via ADB judge ({mode})')
        if sample_info is not None and sample_info.get('usable_for_calibration'):
            self._maybe_start_retrain()

    def _key(self, button, mode):
        return f'{mode}:{getattr(button, "name", str(button))}'

    @staticmethod
    def _fallback_candidate(button, mode):
        if mode != 'match_template_color':
            return {
                'eligible': False,
                'reason': 'mode_not_supported',
                'mode': mode,
            }

        details = getattr(button, '_last_match_template_color', None)
        if not isinstance(details, dict):
            return {
                'eligible': False,
                'reason': 'missing_match_template_color_details',
                'mode': mode,
            }
        eligible = bool(details.get('luma_match') and not details.get('color_match'))
        return {
            'eligible': eligible,
            'reason': 'luma_match_color_mismatch' if eligible else 'not_color_false_negative',
            'mode': mode,
            'details': _jsonable(details),
        }

    def _should_log_miss(self, key, count):
        now = time.time()
        interval = float(self.options.get('adb_miss_log_interval', 30.0))
        last = self.miss_log_time.get(key, 0.0)
        if count <= int(self.options.get('consecutive_failures', 2)) or now - last >= interval:
            self.miss_log_time[key] = now
            return True
        return False

    def _adb_screenshot(self, usb_image):
        now = time.time()
        cache_seconds = float(self.options.get('adb_cache_seconds', 0.75))
        if self.adb_image is not None and now - self.adb_time <= cache_seconds:
            return self.adb_image

        min_interval = float(self.options.get('adb_min_interval', 1.0))
        if now - self.last_adb_attempt < min_interval:
            return None

        self.last_adb_attempt = now
        start = time.time()
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

        end = time.time()
        self.adb_image = image
        self.adb_time = time.time()
        self.adb_meta = {
            'capture_start': start,
            'capture_end': end,
            'time': (start + end) / 2.0,
            'duration': end - start,
        }
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

            start = time.time()
            frame, metadata = get_frame(
                self.config_name,
                timeout=1.0,
                raw=True,
                persistent=True,
                return_metadata=True,
            )
            end = time.time()
            metadata = dict(metadata or {})
            metadata.update({
                'request_start': start,
                'request_end': end,
                'request_duration': end - start,
            })
            return frame, metadata
        except Exception as e:
            marker = f'raw:{type(e).__name__}'
            if marker not in self.warned:
                self.warned.add(marker)
                logger.warning(f'USB capture guard raw USB sample unavailable: {e}')
            return None, None

    @staticmethod
    def _clamp_area(area, image):
        if image is None or not isinstance(area, (list, tuple)) or len(area) != 4:
            return None
        try:
            x0, y0, x1, y1 = [int(v) for v in area]
        except (TypeError, ValueError):
            return None
        height, width = image.shape[:2]
        x0 = max(0, min(width, x0))
        x1 = max(0, min(width, x1))
        y0 = max(0, min(height, y0))
        y1 = max(0, min(height, y1))
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _expanded_quality_area(self, area, image, padding=18, min_size=32):
        area = self._clamp_area(area, image)
        if area is None:
            return None
        x0, y0, x1, y1 = area
        width = x1 - x0
        height = y1 - y0
        extra_x = max(int(padding), (int(min_size) - width + 1) // 2)
        extra_y = max(int(padding), (int(min_size) - height + 1) // 2)
        return self._clamp_area((x0 - extra_x, y0 - extra_y, x1 + extra_x, y1 + extra_y), image)

    @staticmethod
    def _corrcoef(a, b):
        a = a.reshape(-1).astype(np.float32)
        b = b.reshape(-1).astype(np.float32)
        a_std = float(a.std())
        b_std = float(b.std())
        if a_std < 1e-3 or b_std < 1e-3:
            return None
        return float(np.mean(((a - a.mean()) / a_std) * ((b - b.mean()) / b_std)))

    @staticmethod
    def _quality_metrics(usb, adb, area):
        x0, y0, x1, y1 = area
        usb_crop = usb[y0:y1, x0:x1, :3]
        adb_crop = adb[y0:y1, x0:x1, :3]
        if usb_crop.size == 0 or adb_crop.size == 0:
            return None

        usb_gray = cv2.cvtColor(usb_crop, cv2.COLOR_RGB2GRAY).astype(np.float32)
        adb_gray = cv2.cvtColor(adb_crop, cv2.COLOR_RGB2GRAY).astype(np.float32)
        luma_corr = UsbCaptureGuard._corrcoef(usb_gray, adb_gray)

        usb_edge_x = cv2.Sobel(usb_gray, cv2.CV_32F, 1, 0, ksize=3)
        usb_edge_y = cv2.Sobel(usb_gray, cv2.CV_32F, 0, 1, ksize=3)
        adb_edge_x = cv2.Sobel(adb_gray, cv2.CV_32F, 1, 0, ksize=3)
        adb_edge_y = cv2.Sobel(adb_gray, cv2.CV_32F, 0, 1, ksize=3)
        edge_corr = UsbCaptureGuard._corrcoef(
            cv2.magnitude(usb_edge_x, usb_edge_y),
            cv2.magnitude(adb_edge_x, adb_edge_y),
        )

        usb_std = float(usb_gray.std())
        adb_std = float(adb_gray.std())
        if usb_std >= 1e-3 and adb_std >= 1e-3:
            usb_norm = (usb_gray - usb_gray.mean()) / usb_std
            adb_norm = (adb_gray - adb_gray.mean()) / adb_std
            normalized_mae = float(np.mean(np.abs(usb_norm - adb_norm)))
        else:
            normalized_mae = None

        return {
            'area': list(area),
            'edge_correlation': None if edge_corr is None else round(edge_corr, 4),
            'luma_correlation': None if luma_corr is None else round(luma_corr, 4),
            'normalized_luma_mae': None if normalized_mae is None else round(normalized_mae, 4),
            'usb_luma_std': round(usb_std, 4),
            'adb_luma_std': round(adb_std, 4),
        }

    def _sample_training_entries(self, name, metadata):
        metadata = metadata or {}
        entries = []
        if metadata.get('area') is not None:
            entries.append({
                'name': metadata.get('button_name') or metadata.get('button') or name,
                'area': metadata.get('area'),
                'color': metadata.get('color'),
                'source': 'button_area',
            })
        for index, item in enumerate(metadata.get('focus_areas') or []):
            if not isinstance(item, dict):
                continue
            entries.append({
                'name': item.get('name') or f'{name}_{index + 1}',
                'area': item.get('area'),
                'color': item.get('color'),
                'source': 'focus_area',
            })
        return entries

    def _sample_quality(self, name, metadata, usb_image, adb_image, raw_image, raw_metadata):
        metadata = metadata or {}
        reference_usb = raw_image if raw_image is not None else usb_image
        area_results = []
        usable_area_count = 0
        for entry in self._sample_training_entries(name, metadata):
            area = self._expanded_quality_area(entry.get('area'), reference_usb)
            if area is None:
                continue
            metrics = self._quality_metrics(reference_usb, adb_image, area)
            if metrics is None:
                continue
            edge_corr = metrics.get('edge_correlation')
            luma_corr = metrics.get('luma_correlation')
            normalized_mae = metrics.get('normalized_luma_mae')
            passed = (
                (edge_corr is not None and edge_corr >= float(self.options.get('sample_min_edge_correlation', 0.20)))
                or (luma_corr is not None and luma_corr >= float(self.options.get('sample_min_luma_correlation', 0.30)))
                or (normalized_mae is not None and normalized_mae <= float(self.options.get('sample_max_normalized_luma_mae', 0.90)))
            )
            metrics.update({
                'name': entry.get('name'),
                'source': entry.get('source'),
                'training_area': _jsonable(entry.get('area')),
                'passed': bool(passed),
            })
            area_results.append(metrics)
            if passed:
                usable_area_count += 1

        adb_meta = self.adb_meta or {}
        raw_time = (raw_metadata or {}).get('time')
        adb_time = adb_meta.get('time')
        pair_delay = None
        time_passed = raw_image is not None
        if raw_time is not None and adb_time is not None:
            try:
                pair_delay = abs(float(raw_time) - float(adb_time))
                time_passed = pair_delay <= float(self.options.get('sample_max_pair_delay', 1.5))
            except (TypeError, ValueError):
                pair_delay = None

        usable = bool(raw_image is not None and time_passed and usable_area_count)
        return {
            'usable_for_calibration': usable,
            'reason': None if usable else 'raw_unavailable' if raw_image is None else 'time_mismatch' if not time_passed else 'unstable_or_unmatched_roi',
            'pair_delay_seconds': None if pair_delay is None else round(pair_delay, 4),
            'raw_usb_metadata': _jsonable(raw_metadata),
            'adb_metadata': _jsonable(adb_meta),
            'quality_thresholds': {
                'max_pair_delay': float(self.options.get('sample_max_pair_delay', 1.5)),
                'min_edge_correlation': float(self.options.get('sample_min_edge_correlation', 0.20)),
                'min_luma_correlation': float(self.options.get('sample_min_luma_correlation', 0.30)),
                'max_normalized_luma_mae': float(self.options.get('sample_max_normalized_luma_mae', 0.90)),
            },
            'areas': area_results,
            'usable_area_count': usable_area_count,
            'training_entries': _jsonable(self._sample_training_entries(name, metadata)),
        }

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
            'usb_candidate': self._fallback_candidate(button, mode),
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

        raw_image, raw_metadata = self._capture_raw_usb()
        metadata = _jsonable(metadata or {})
        sample_quality = self._sample_quality(
            name,
            metadata,
            usb_image=usb_image,
            adb_image=adb_image,
            raw_image=raw_image,
            raw_metadata=raw_metadata,
        )
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
                'task': str(getattr(self.config, 'task', None)),
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
                data.update(metadata)
            data['sample_quality'] = sample_quality
            data = _jsonable(data)

            with open(os.path.join(sample_dir, 'metadata.json'), 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
            self._prune_samples()
            if raw_image is not None and sample_quality.get('usable_for_calibration'):
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
                    sample_quality=sample_quality,
                )
            else:
                write_usb_capture_guard_event(
                    self.config_name,
                    'auto_calibration_sample_rejected',
                    'Saved fallback sample for diagnostics, but it is unsafe for USB color calibration',
                    target=name,
                    mode=mode,
                    sample=sample_dir.replace('\\', '/'),
                    metadata=metadata,
                    raw_available=raw_image is not None,
                    sample_quality=sample_quality,
                )
            return {
                'path': sample_dir.replace('\\', '/'),
                'raw_available': raw_image is not None,
                'usable_for_calibration': bool(sample_quality.get('usable_for_calibration')),
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
