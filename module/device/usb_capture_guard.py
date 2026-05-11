# USB capture near-threshold recognition guard.
#
# Keep this layer deliberately narrow: it only arbitrates template-color
# failures that are already very close to passing on USB capture. Normal false
# checks must stay cheap and untouched.
import json
import os
import time
from datetime import datetime

from module.base.utils import color_similarity, get_color
from module.logger import logger


LUMA_MARGIN = 0.025
COLOR_MARGIN = 6
ADB_CACHE_SECONDS = 0.75
ADB_MIN_INTERVAL = 1.0
LOG_PATH = os.path.join('log', 'usb_capture_guard.log')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def _safe_float(value):
    if value is None:
        return None
    return round(float(value), 6)


def _safe_color(value):
    if value is None:
        return None
    return [_safe_float(channel) for channel in value]


def _task_name(config):
    task = getattr(config, 'task', None)
    return getattr(task, 'command', None) or getattr(task, 'name', None)


def _write_guard_log(payload):
    payload = dict(payload)
    payload.setdefault('time', _now())
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
    except Exception as e:
        logger.warning(f'Failed to write USB capture guard log: {e}')


def _is_usb_capture(main):
    return getattr(main.config, 'Emulator_ScreenshotMethod', None) == 'usb_capture'


def _near_threshold_reason(button, similarity, threshold):
    luma_sim = getattr(button, '_match_luma_similarity', None)
    luma_ok = getattr(button, '_match_template_color_luma_ok', None)
    color_diff = getattr(button, '_match_template_color_color_diff', None)

    if luma_sim is None:
        return None

    if not luma_ok:
        distance = similarity - luma_sim
        if 0 <= distance <= LUMA_MARGIN:
            return 'luma_near_threshold'
        return None

    if color_diff is not None:
        distance = color_diff - threshold
        if 0 < distance <= COLOR_MARGIN:
            return 'color_near_threshold'

    return None


def _appear_on_near_threshold(button, image, threshold):
    try:
        observed = get_color(image, button.area)
        expected = button.color
        diff = color_similarity(observed, expected)
    except Exception:
        return None, None, None

    if 0 < diff - threshold <= COLOR_MARGIN:
        return 'color_near_threshold', observed, diff
    return None, observed, diff


def _adb_cached_screenshot(device):
    now = time.time()
    image = getattr(device, '_usb_capture_guard_adb_image', None)
    image_time = getattr(device, '_usb_capture_guard_adb_image_time', 0)
    if image is not None and now - image_time <= ADB_CACHE_SECONDS:
        return image, 'cache', now - image_time

    last_request = getattr(device, '_usb_capture_guard_adb_last_request', 0)
    if now - last_request < ADB_MIN_INTERVAL:
        return None, 'rate_limited', now - last_request

    device._usb_capture_guard_adb_last_request = now
    image = device.screenshot_adb()
    device._usb_capture_guard_adb_image = image
    device._usb_capture_guard_adb_image_time = time.time()
    return image, 'fresh', 0


def appear_on(main, button, threshold):
    """
    Return True only when ADB confirms a USB near-threshold color false negative.
    """
    if not _is_usb_capture(main):
        return False

    reason, observed, diff = _appear_on_near_threshold(button, main.device.image, threshold)
    if reason is None:
        return False

    payload = {
        'event': 'near_threshold_fallback',
        'config': getattr(main.config, 'config_name', None),
        'task': _task_name(main.config),
        'button': getattr(button, 'name', str(button)),
        'mode': 'appear_on',
        'reason': reason,
        'usb_color': _safe_color(observed),
        'expected_color': _safe_color(getattr(button, 'color', None)),
        'usb_color_diff': _safe_float(diff),
        'color_threshold': threshold,
    }

    try:
        adb_image, source, age = _adb_cached_screenshot(main.device)
        payload['adb_source'] = source
        payload['adb_age_seconds'] = _safe_float(age)
        if adb_image is None:
            payload['result'] = 'skip'
            _write_guard_log(payload)
            return False

        appear = button.appear_on(adb_image, threshold=threshold)
        _, adb_color, adb_diff = _appear_on_near_threshold(button, adb_image, threshold)
        payload['adb_color'] = _safe_color(adb_color)
        payload['adb_color_diff'] = _safe_float(adb_diff)
        payload['result'] = 'hit' if appear else 'miss'
        _write_guard_log(payload)

        if appear:
            logger.info(
                f'USB capture guard confirmed {button.name}: '
                f'{reason}, usb_diff={payload["usb_color_diff"]}, '
                f'adb_diff={payload["adb_color_diff"]}')
            return True

        return False
    except Exception as e:
        payload['result'] = 'error'
        payload['error'] = repr(e)
        _write_guard_log(payload)
        logger.warning(f'USB capture guard failed for {button.name}: {e}')
        return False


def match_template_color(main, button, offset, similarity, threshold):
    """
    Return True only when ADB confirms a USB near-threshold false negative.

    Args:
        main: Base-like object with config/device.
        button: Button just checked by Button.match_template_color().
        offset: Original matching offset.
        similarity: Original luma threshold.
        threshold: Original color threshold.

    Returns:
        bool: Whether ADB fallback confirms the button.
    """
    if not _is_usb_capture(main):
        return False

    reason = _near_threshold_reason(button, similarity=similarity, threshold=threshold)
    if reason is None:
        return False

    old_offset = getattr(button, '_button_offset', None)
    payload = {
        'event': 'near_threshold_fallback',
        'config': getattr(main.config, 'config_name', None),
        'task': _task_name(main.config),
        'button': getattr(button, 'name', str(button)),
        'reason': reason,
        'usb_luma_similarity': _safe_float(getattr(button, '_match_luma_similarity', None)),
        'luma_threshold': _safe_float(similarity),
        'usb_color_diff': _safe_float(getattr(button, '_match_template_color_color_diff', None)),
        'color_threshold': threshold,
    }

    try:
        adb_image, source, age = _adb_cached_screenshot(main.device)
        payload['adb_source'] = source
        payload['adb_age_seconds'] = _safe_float(age)
        if adb_image is None:
            payload['result'] = 'skip'
            _write_guard_log(payload)
            return False

        appear = button.match_template_color(
            adb_image, offset=offset, similarity=similarity, threshold=threshold)
        payload['adb_luma_similarity'] = _safe_float(getattr(button, '_match_luma_similarity', None))
        payload['adb_color_diff'] = _safe_float(getattr(button, '_match_template_color_color_diff', None))
        payload['result'] = 'hit' if appear else 'miss'
        _write_guard_log(payload)

        if appear:
            logger.info(
                f'USB capture guard confirmed {button.name}: '
                f'{reason}, usb_sim={payload["usb_luma_similarity"]}, '
                f'adb_sim={payload["adb_luma_similarity"]}')
            return True

        button._button_offset = old_offset
        return False
    except Exception as e:
        button._button_offset = old_offset
        payload['result'] = 'error'
        payload['error'] = repr(e)
        _write_guard_log(payload)
        logger.warning(f'USB capture guard failed for {button.name}: {e}')
        return False
