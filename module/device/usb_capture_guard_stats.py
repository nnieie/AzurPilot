import json
import os
import time


INVALID_PATH_CHARS = set('<>:"/\\|?*')
MAX_EVENT_LOG_BYTES = 5 * 1024 * 1024


def safe_name(value, fallback='unknown'):
    text = str(value or fallback)
    text = ''.join('_' if char in INVALID_PATH_CHARS or ord(char) < 32 else char for char in text)
    text = '_'.join(text.split()).strip('._ ')
    return text or fallback


def stats_path(config_name):
    return os.path.join('log', 'usb_capture_guard', safe_name(config_name), 'stats.json')


def event_log_path():
    return os.path.join('log', 'usb_capture_guard.log')


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _today():
    return time.strftime('%Y-%m-%d')


def _empty_counter(extra=None):
    data = {
        'total': 0,
        'today': 0,
        'date': _today(),
        'last_time': None,
    }
    if extra:
        data.update(extra)
    return data


def default_stats():
    return {
        'fallback': _empty_counter(),
        'auto_calibration': _empty_counter(),
        'auto_calibration_success': _empty_counter(),
    }


def load_usb_capture_guard_stats(config_name):
    data = default_stats()
    path = stats_path(config_name)
    if not os.path.exists(path):
        return data
    try:
        with open(path, 'r', encoding='utf-8') as file:
            loaded = json.load(file)
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if isinstance(value, dict):
                    data.setdefault(key, _empty_counter()).update(value)
    except Exception:
        pass
    return data


def save_usb_capture_guard_stats(config_name, data):
    path = stats_path(config_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def record_usb_capture_guard_stat(config_name, key):
    try:
        data = load_usb_capture_guard_stats(config_name)
        counter = data.setdefault(key, _empty_counter())
        today = _today()
        if counter.get('date') != today:
            counter['date'] = today
            counter['today'] = 0
        counter['total'] = int(counter.get('total') or 0) + 1
        counter['today'] = int(counter.get('today') or 0) + 1
        counter['last_time'] = _now()
        save_usb_capture_guard_stats(config_name, data)
    except Exception:
        pass


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        return value.tolist()
    except Exception:
        return str(value)


def write_usb_capture_guard_event(config_name, event, message='', **fields):
    try:
        path = event_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > MAX_EVENT_LOG_BYTES:
            os.replace(path, f'{path}.1')
        data = {
            'time': _now(),
            'config': config_name,
            'event': event,
        }
        if message:
            data['message'] = str(message)
        for key, value in fields.items():
            if value is None:
                continue
            data[key] = _jsonable(value)
        with open(path, 'a', encoding='utf-8') as file:
            file.write(json.dumps(data, ensure_ascii=False, sort_keys=False) + '\n')
    except Exception:
        pass


def _format_counter(counter):
    total = int(counter.get('total') or 0)
    today = int(counter.get('today') or 0) if counter.get('date') == _today() else 0
    last_time = counter.get('last_time') or '从未'
    return f'总共触发 {total} 次，今天触发 {today} 次，最近一次：{last_time}'


def format_usb_capture_guard_help(config_name, arg_name, help_text):
    if arg_name not in ('UsbCaptureAdbFallback', 'UsbCaptureAutoCalibration'):
        return help_text

    stats = load_usb_capture_guard_stats(config_name)
    if arg_name == 'UsbCaptureAdbFallback':
        line = _format_counter(stats.get('fallback', {}))
    else:
        line = _format_counter(stats.get('auto_calibration', {}))
        success = stats.get('auto_calibration_success', {})
        line += f'；成功更新 {int(success.get("total") or 0)} 次'

    if help_text:
        return f'{help_text}\n统计：{line}'
    return f'统计：{line}'
