"""
Capture paired USB raw and ADB screenshots for color calibration.

This is a data collection helper only. It does not write calibration files.
When the USB capture service is running, it asks the service for raw frames so
the capture card is not opened twice. Otherwise it opens the capture device
directly using the current config.

Examples:
    toolkit\\python.exe dev_tools\\usb_capture_pair_capture.py --config-name "alas (1)"
    toolkit\\python.exe dev_tools\\usb_capture_pair_capture.py --config-name "alas (1)" --count 5 --label shop_yellow_book
"""

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np

from dev_tools.usb_capture_color_calibrate import (
    capture_adb,
    capture_usb,
    metrics,
    save_image,
)
from dev_tools.usb_capture_service import (
    apply_usb_color_correction,
    get_frame,
    load_usb_color_correction,
    ping_service,
)


def parse_args():
    parser = argparse.ArgumentParser(description='Capture paired USB raw and ADB screenshots.')
    parser.add_argument('--config-name', default=os.environ.get('ALAS_CONFIG_NAME', 'alas'))
    parser.add_argument('--label', default='sample', help='Short label used in filenames. Default: sample')
    parser.add_argument('--count', type=int, default=1, help='Number of pairs to capture. Default: 1')
    parser.add_argument('--interval', type=float, default=0.0, help='Seconds to wait between pairs. Default: 0')
    parser.add_argument('--output-dir', default='log/usb_capture_pairs', help='Output root directory.')
    parser.add_argument('--warmup', type=int, default=5, help='Direct-capture warmup frames if service is not running.')
    parser.add_argument('--no-service', action='store_true', help='Open USB capture directly instead of using service.')
    parser.add_argument('--no-corrected', action='store_true', help='Skip saving current-LUT corrected USB preview.')
    parser.add_argument('--no-prompt', action='store_true', help='Capture immediately without pressing Enter.')
    return parser.parse_args()


def safe_name(value):
    value = str(value or 'sample')
    invalid = '<>:"/\\|?*\x00'
    for char in invalid:
        value = value.replace(char, '_')
    return value.strip(' .') or 'sample'


def capture_usb_raw(args):
    if not args.no_service and ping_service(args.config_name):
        frame, meta = get_frame(
            args.config_name,
            raw=True,
            return_metadata=True,
            persistent=True,
        )
        return frame, {
            'source': 'usb_capture_service',
            'service_raw': True,
            'service': meta,
        }

    frame = capture_usb(args)
    return frame, {
        'source': 'direct_usb_capture',
        'service_raw': False,
        'warmup': args.warmup,
    }


def corrected_from_raw(config_name, raw):
    correction = load_usb_color_correction(config_name, use_c_accel=False)
    if correction is None:
        return None, None
    corrected = apply_usb_color_correction(raw, correction)
    return corrected, {
        'path': correction.get('path'),
        'model': correction.get('model'),
    }


def image_metrics(name, image, target):
    if image is None or target is None:
        return None
    if image.shape != target.shape:
        return {
            'error': f'shape mismatch: image={list(image.shape)}, target={list(target.shape)}',
        }
    return metrics(name, image, target)


def capture_pair(args, folder, index):
    label = safe_name(args.label)
    prefix = f'{index:02d}_{label}_{time.strftime("%Y%m%d_%H%M%S")}'

    start = time.time()
    usb_raw, usb_meta = capture_usb_raw(args)
    usb_elapsed = (time.time() - start) * 1000

    start = time.time()
    adb = capture_adb(args.config_name)
    adb_elapsed = (time.time() - start) * 1000

    corrected = None
    correction_meta = None
    if not args.no_corrected:
        corrected, correction_meta = corrected_from_raw(args.config_name, usb_raw)

    usb_raw_path = os.path.join(folder, f'{prefix}_usb_raw.png')
    adb_path = os.path.join(folder, f'{prefix}_adb.png')
    corrected_path = os.path.join(folder, f'{prefix}_usb_corrected.png') if corrected is not None else None
    meta_path = os.path.join(folder, f'{prefix}_meta.json')

    save_image(usb_raw_path, usb_raw)
    save_image(adb_path, adb)
    if corrected is not None:
        save_image(corrected_path, corrected)

    meta = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config_name': args.config_name,
        'label': args.label,
        'index': index,
        'files': {
            'usb_raw': usb_raw_path.replace('\\', '/'),
            'adb': adb_path.replace('\\', '/'),
            'usb_corrected': corrected_path.replace('\\', '/') if corrected_path else None,
        },
        'capture_ms': {
            'usb_raw': round(usb_elapsed, 3),
            'adb': round(adb_elapsed, 3),
        },
        'usb': usb_meta,
        'correction': correction_meta,
        'metrics': {
            'raw_vs_adb': image_metrics('USB raw vs ADB', usb_raw, adb),
            'corrected_vs_adb': image_metrics('USB corrected vs ADB', corrected, adb),
        },
        'shapes': {
            'usb_raw': list(usb_raw.shape),
            'adb': list(adb.shape),
            'usb_corrected': list(corrected.shape) if corrected is not None else None,
        },
    }

    # Convert numpy scalars left by metric helpers, if any.
    def default(obj):
        if isinstance(obj, np.generic):
            return obj.item()
        raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')

    with open(meta_path, 'w', encoding='utf-8') as file:
        json.dump(meta, file, indent=2, ensure_ascii=False, default=default)

    print(f'[{index}] wrote:')
    print(f'  USB raw      : {usb_raw_path}')
    print(f'  ADB          : {adb_path}')
    if corrected_path:
        print(f'  USB corrected: {corrected_path}')
    print(f'  meta         : {meta_path}')


def main():
    args = parse_args()
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    folder = os.path.join(args.output_dir, safe_name(args.config_name), f'{timestamp}_{safe_name(args.label)}')
    os.makedirs(folder, exist_ok=True)

    print(f'Output: {folder}')
    print('Put the game on a stable target page, then capture.')

    for index in range(1, max(1, args.count) + 1):
        if not args.no_prompt:
            input(f'[{index}/{args.count}] Press Enter to capture USB raw + ADB pair...')
        capture_pair(args, folder, index)
        if index < args.count and args.interval > 0:
            time.sleep(args.interval)

    print('Done.')


if __name__ == '__main__':
    main()
