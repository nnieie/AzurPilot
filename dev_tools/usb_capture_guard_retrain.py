"""
Fit a USB color calibration candidate from USB capture guard failure samples.

This tool consumes paired USB/ADB screenshots written by
module.device.usb_capture_guard and, when available, previous calibration
snapshots under log/usb_color_calibration/<config-name>. It evaluates a
candidate LUT first; --apply writes config/usb_color/<config-name>.json only
when safety checks pass, unless --force is supplied.
"""

import argparse
import glob
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cv2
import numpy as np

from dev_tools.usb_capture_color_calibrate import (
    apply_lut3d,
    apply_patch_model,
    choose_asset_correction,
    ensure_output_size,
    expand_weighted_samples,
    fit_channel_luts,
    fit_lut3d,
    max_channel_delta,
    mean_rect,
    metrics,
    metrics_pixels,
    parse_button_assets,
    patch_safety_check,
    sample_visible_assets,
    server_key_from_config,
    validate_asset_results,
)
from dev_tools.usb_capture_service import color_correction_path, legacy_color_correction_path
from module.device.usb_capture_guard_stats import record_usb_capture_guard_stat, write_usb_capture_guard_event


INVALID_PATH_CHARS = set('<>:"/\\|?*')


def parse_args():
    parser = argparse.ArgumentParser(description='Retrain USB capture color calibration from guard samples.')
    parser.add_argument('--config-name', default=os.environ.get('ALAS_CONFIG_NAME', 'alas'))
    parser.add_argument('--output', default=None, help='Default: config/usb_color/<config-name>.json')
    parser.add_argument('--dry-run', action='store_true', help='Evaluate only. This is the default if --apply is omitted.')
    parser.add_argument('--apply', action='store_true', help='Write the calibration when safety checks pass.')
    parser.add_argument('--force', action='store_true', help='Write even when safety checks fail.')
    parser.add_argument('--model', choices=('lut3d', 'channel_lut'), default='lut3d')
    parser.add_argument('--lut3d-size', type=int, default=33)
    parser.add_argument('--lut3d-power', type=float, default=2.0)
    parser.add_argument('--sample-root', default=None, help='Default: log/usb_capture_guard/<config-name>')
    parser.add_argument('--max-guard-samples', type=int, default=80)
    parser.add_argument('--include-calibration-snapshots', action='store_true', default=True)
    parser.add_argument('--no-calibration-snapshots', action='store_false', dest='include_calibration_snapshots')
    parser.add_argument('--max-calibration-pairs', type=int, default=12)
    parser.add_argument('--allow-corrected-usb', action='store_true',
                        help='Use guard samples without usb_raw.png. Not recommended for --apply.')
    parser.add_argument('--allow-unqualified-guard-samples', action='store_true',
                        help='Use old guard samples without sample_quality.usable_for_calibration=true. Not recommended for --apply.')
    parser.add_argument('--guard-include-visible-assets', action='store_true',
                        help='Also use globally visible Button assets from guard sample screenshots. Disabled by default to avoid dynamic-frame contamination.')
    parser.add_argument('--asset-match-threshold', type=float, default=10.0)
    parser.add_argument('--asset-weight', type=float, default=24.0)
    parser.add_argument('--asset-failed-weight-multiplier', type=float, default=3.0)
    parser.add_argument('--asset-pixel-samples', type=int, default=32)
    parser.add_argument('--asset-min-samples', type=int, default=8)
    parser.add_argument('--asset-max-buttons', type=int, default=0)
    parser.add_argument('--guard-weight', type=float, default=72.0)
    parser.add_argument('--guard-pixel-samples', type=int, default=48)
    parser.add_argument('--no-asset-auto-dampen', action='store_true')
    parser.add_argument('--max-worse-ratio', type=float, default=1.05)
    parser.add_argument('--max-worse-delta', type=float, default=1.0)
    parser.add_argument('--protected-color-delta', type=float, default=32.0)
    return parser.parse_args()


def safe_name(value):
    text = str(value or 'alas')
    text = ''.join('_' if char in INVALID_PATH_CHARS or ord(char) < 32 else char for char in text)
    text = '_'.join(text.split()).strip('._ ')
    return text or 'alas'


def load_rgb(path):
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f'Unable to load image: {path}')
    return ensure_output_size(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def resolve_path(path):
    if not path:
        return None
    path = os.path.normpath(path)
    if os.path.isabs(path):
        return path
    return os.path.join(REPO_ROOT, path)


def tuple_ints(value, length):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    try:
        return tuple(int(v) for v in value)
    except (TypeError, ValueError):
        return None


def default_sample_root(config_name):
    return os.path.join('log', 'usb_capture_guard', safe_name(config_name))


def load_guard_pairs(args):
    root = args.sample_root or default_sample_root(args.config_name)
    root = resolve_path(root)
    metas = glob.glob(os.path.join(root, '**', 'metadata.json'), recursive=True)
    metas.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    pairs = []
    skipped_no_raw = 0
    skipped_unqualified = 0
    limit = max(0, args.max_guard_samples)
    for meta_path in metas:
        if limit and len(pairs) >= limit:
            break
        with open(meta_path, 'r', encoding='utf-8') as file:
            meta = json.load(file)
        quality = meta.get('sample_quality') or {}
        if not args.allow_unqualified_guard_samples and quality.get('usable_for_calibration') is not True:
            skipped_unqualified += 1
            continue
        images = meta.get('images', {})
        usb_key = 'usb_raw' if images.get('usb_raw') else 'usb'
        if usb_key != 'usb_raw' and not args.allow_corrected_usb:
            skipped_no_raw += 1
            continue
        adb_path = resolve_path(images.get('adb'))
        usb_path = resolve_path(images.get(usb_key))
        if not (adb_path and usb_path and os.path.exists(adb_path) and os.path.exists(usb_path)):
            continue
        pairs.append({
            'source': 'guard',
            'metadata': meta,
            'adb': load_rgb(adb_path),
            'usb': load_rgb(usb_path),
            'usb_source': usb_key,
        })
    if skipped_no_raw:
        print(f'Skipped {skipped_no_raw} guard samples without usb_raw.png; use --allow-corrected-usb to include them.')
    if skipped_unqualified:
        print(f'Skipped {skipped_unqualified} guard samples that were not marked safe for calibration.')
    return pairs


def load_calibration_pairs(args):
    if not args.include_calibration_snapshots:
        return []
    root = resolve_path(os.path.join('log', 'usb_color_calibration', args.config_name.replace(os.sep, '_')))
    adb_paths = glob.glob(os.path.join(root, '**', 'capture_*_adb.png'), recursive=True)
    adb_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    pairs = []
    for adb_path in adb_paths[:max(0, args.max_calibration_pairs)]:
        usb_path = adb_path.replace('_adb.png', '_usb_raw.png')
        if not os.path.exists(usb_path):
            continue
        pairs.append({
            'source': 'calibration',
            'metadata': {},
            'adb': load_rgb(adb_path),
            'usb': load_rgb(usb_path),
            'usb_source': 'usb_raw',
        })
    return pairs


def guard_focus_area_sample(pair, args, area, name, color=None):
    area = tuple_ints(area, 4)
    if area is None:
        return None
    x0, y0, x1, y1 = area
    width, height = pair['usb'].shape[1], pair['usb'].shape[0]
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return None

    color = tuple_ints(color, 3)
    adb_mean = mean_rect(pair['adb'], area)
    usb_mean = mean_rect(pair['usb'], area)
    expected = np.asarray(color, dtype=np.float32) if color is not None else adb_mean
    adb_delta = max_channel_delta(adb_mean, expected)
    usb_delta = max_channel_delta(usb_mean, expected)
    result = {
        'name': 'GUARD_' + safe_name(name),
        'source': 'usb_capture_guard',
        'area': list(area),
        'pixels': int((x1 - x0) * (y1 - y0)),
        'expected': expected.round(3).tolist(),
        'adb': adb_mean.round(3).tolist(),
        'usb': usb_mean.round(3).tolist(),
        'adb_delta': round(adb_delta, 3),
        'usb_delta': round(usb_delta, 3),
        'weight': round(float(args.guard_weight), 3),
        'pixel_samples': 0,
        'usb_pass': usb_delta <= args.asset_match_threshold,
    }

    usb_values = [usb_mean]
    adb_values = [adb_mean]
    weights = [float(args.guard_weight)]
    pixel_count = min(int(args.guard_pixel_samples), (x1 - x0) * (y1 - y0))
    if pixel_count > 0:
        usb_region = pair['usb'][y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
        adb_region = pair['adb'][y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
        indexes = np.linspace(0, usb_region.shape[0] - 1, pixel_count, dtype=np.int32)
        usb_values.extend(usb_region[indexes])
        adb_values.extend(adb_region[indexes])
        weights.extend([max(1.0, float(args.guard_weight) / 6.0)] * pixel_count)
        result['pixel_samples'] = int(pixel_count)

    return (
        np.asarray(usb_values, dtype=np.float32),
        np.asarray(adb_values, dtype=np.float32),
        np.asarray(weights, dtype=np.float32),
        result,
    )


def _area_key(area):
    area = tuple_ints(area, 4)
    return None if area is None else tuple(area)


def guard_quality_area_passed(meta, area):
    quality = meta.get('sample_quality') or {}
    quality_areas = quality.get('areas') or []
    if not quality_areas:
        return True
    area_key = _area_key(area)
    if area_key is None:
        return False
    for item in quality_areas:
        if _area_key(item.get('training_area')) == area_key:
            return bool(item.get('passed'))
    return False


def guard_focus_sample(pair, args):
    meta = pair.get('metadata') or {}
    entries = []
    if meta.get('area') is not None:
        entries.append({
            'name': meta.get('button_name') or meta.get('button') or meta.get('name') or 'BUTTON',
            'area': meta.get('area'),
            'color': meta.get('color'),
        })
    for index, item in enumerate(meta.get('focus_areas') or []):
        if not isinstance(item, dict):
            continue
        entries.append({
            'name': item.get('name') or f'{meta.get("name", "FOCUS")}_{index + 1}',
            'area': item.get('area'),
            'color': item.get('color'),
        })

    usb_sets = []
    adb_sets = []
    weight_sets = []
    samples = []
    for entry in entries:
        if not guard_quality_area_passed(meta, entry.get('area')):
            continue
        focus = guard_focus_area_sample(
            pair,
            args,
            area=entry.get('area'),
            name=entry.get('name'),
            color=entry.get('color'),
        )
        if focus is None:
            continue
        usb_values, adb_values, weights, sample = focus
        usb_sets.append(usb_values)
        adb_sets.append(adb_values)
        weight_sets.append(weights)
        samples.append(sample)

    if not samples:
        return None
    return (
        np.concatenate(usb_sets, axis=0),
        np.concatenate(adb_sets, axis=0),
        np.concatenate(weight_sets, axis=0),
        samples,
    )


def build_captures(args, pairs, assets):
    captures = []
    usb_sets = []
    adb_sets = []
    weight_sets = []
    for index, pair in enumerate(pairs, 1):
        if pair['source'] == 'guard':
            if args.guard_include_visible_assets:
                usb_values, adb_values, weights, samples = sample_visible_assets(pair['adb'], pair['usb'], assets, args)
            else:
                usb_values = np.empty((0, 3), dtype=np.float32)
                adb_values = np.empty((0, 3), dtype=np.float32)
                weights = np.empty((0,), dtype=np.float32)
                samples = []
            focus = guard_focus_sample(pair, args)
            if focus is not None:
                focus_usb, focus_adb, focus_weights, focus_samples = focus
                usb_values = np.concatenate([usb_values, focus_usb], axis=0) if usb_values.size else focus_usb
                adb_values = np.concatenate([adb_values, focus_adb], axis=0) if adb_values.size else focus_adb
                weights = np.concatenate([weights, focus_weights], axis=0) if weights.size else focus_weights
                samples.extend(focus_samples)
        else:
            usb_values, adb_values, weights, samples = sample_visible_assets(pair['adb'], pair['usb'], assets, args)

        captures.append({
            'index': index,
            'source': pair['source'],
            'usb_source': pair['usb_source'],
            'adb': pair['adb'],
            'usb': pair['usb'],
            'samples': samples,
            'sample_quality': (pair.get('metadata') or {}).get('sample_quality'),
        })
        if len(samples):
            usb_sets.append(usb_values)
            adb_sets.append(adb_values)
            weight_sets.append(weights)
        print(
            f'Pair {index}: source={pair["source"]}, usb={pair["usb_source"]}, '
            f'visible={len(samples)}, fit_samples={usb_values.shape[0] if usb_values.size else 0}'
        )
    return captures, usb_sets, adb_sets, weight_sets


def fit_correction(args, usb_all, adb_all, weights_all):
    if args.model == 'channel_lut':
        fit_usb, fit_adb = expand_weighted_samples(usb_all, adb_all, weights_all)
        print(f'Fitting per-channel LUT with {usb_all.shape[0]} samples ({fit_usb.shape[0]} weighted)...')
        return fit_channel_luts(fit_usb, fit_adb)

    print(f'Fitting 3D LUT with {usb_all.shape[0]} weighted samples...')
    return fit_lut3d(
        usb_all,
        adb_all,
        size=args.lut3d_size,
        power=args.lut3d_power,
        sample_weights=weights_all,
    )


def corrected_sample_pixels(args, correction, usb_all):
    if args.model == 'channel_lut':
        return np.stack([
            correction[channel][np.clip(np.rint(usb_all[:, channel]), 0, 255).astype(np.uint8)]
            for channel in range(3)
        ], axis=1).astype(np.float32)
    return apply_lut3d(
        np.clip(np.rint(usb_all), 0, 255).astype(np.uint8).reshape(-1, 1, 3),
        correction,
    ).reshape(-1, 3).astype(np.float32)


def evaluate(args, captures, correction, usb_all, adb_all):
    before = metrics_pixels('Guard fit before', usb_all, adb_all)
    after = metrics_pixels('Guard fit after ', corrected_sample_pixels(args, correction, usb_all), adb_all)

    capture_results = []
    total_before_pass = 0
    total_after_pass = 0
    total_visible = 0
    regressions = []
    guard_still_off = []

    for capture in captures:
        corrected = apply_patch_model(capture['usb'], args.model, correction)
        print(f'Pair {capture["index"]} full-frame metrics:')
        full_before = metrics('  Before', capture['usb'], capture['adb'])
        full_after = metrics('  After ', corrected, capture['adb'])
        validation = validate_asset_results(capture['usb'], corrected, capture['samples'], args)
        total_visible += validation['visible']
        total_before_pass += validation['before_pass']
        total_after_pass += validation['after_pass']
        regressions.extend(f'pair {capture["index"]}: {name}' for name in validation['regressions'])
        for item in validation['failures']:
            if item['name'].startswith('GUARD_'):
                guard_still_off.append(f'pair {capture["index"]}: {item["name"]}')
        print(
            f'Pair {capture["index"]} asset checks: visible={validation["visible"]}, '
            f'before={validation["before_pass"]}, after={validation["after_pass"]}, '
            f'fixed={len(validation["fixed"])}, regressions={len(validation["regressions"])}'
        )
        capture_results.append({
            'index': capture['index'],
            'source': capture['source'],
            'usb_source': capture['usb_source'],
            'visible_assets': len(capture['samples']),
            'sample_quality': capture.get('sample_quality'),
            'samples': capture['samples'],
            'metrics': {
                'before': full_before,
                'after': full_after,
            },
            'asset_validation': validation,
        })

    patch_capture_results = [
        item for item in capture_results
        if item.get('source') != 'guard'
    ]
    safety_failures = []
    if after['mae'] >= before['mae']:
        safety_failures.append(f'fit MAE did not improve: {before["mae"]:.2f} -> {after["mae"]:.2f}')
    if total_after_pass < total_before_pass:
        safety_failures.append(f'asset pass count worsened: {total_before_pass} -> {total_after_pass}')
    if regressions:
        safety_failures.append('asset regressions: ' + ', '.join(regressions[:20]))
    if guard_still_off:
        safety_failures.append('guard samples still off: ' + ', '.join(guard_still_off[:20]))
    safety_failures.extend(patch_safety_check(args, args.model, correction, before, after, patch_capture_results))

    return {
        'before': before,
        'after': after,
        'total_visible': total_visible,
        'total_before_pass': total_before_pass,
        'total_after_pass': total_after_pass,
        'capture_results': capture_results,
        'safety_failures': safety_failures,
    }


def build_output_data(args, captures, assets, correction, evaluation, strength):
    data = {
        'enabled': True,
        'config_name': args.config_name,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'model': args.model,
        'asset_fit': True,
        'guard_retrain': True,
        'captures': len(captures),
        'assets_loaded': len(assets),
        'assets': {
            'visible': evaluation['total_visible'],
            'before_pass': evaluation['total_before_pass'],
            'after_pass': evaluation['total_after_pass'],
            'match_threshold': args.asset_match_threshold,
            'base_weight': args.asset_weight,
            'failed_weight_multiplier': args.asset_failed_weight_multiplier,
            'pixel_samples_per_failed_asset': args.asset_pixel_samples,
            'guard_weight': args.guard_weight,
            'guard_pixel_samples': args.guard_pixel_samples,
            'lut_strength': strength,
            'guard_sample_quality_required': not args.allow_unqualified_guard_samples,
            'guard_visible_assets_enabled': bool(args.guard_include_visible_assets),
        },
        'metrics': {
            'before': evaluation['before'],
            'after': evaluation['after'],
        },
        'capture_results': evaluation['capture_results'],
    }
    if args.model == 'channel_lut':
        data['luts'] = correction.tolist()
    else:
        data['lut_size'] = int(correction.shape[0])
        data['lut'] = correction.tolist()
    failures = evaluation['safety_failures']
    data['safety'] = {'passed': not failures, 'failures': failures}
    return data


def write_calibration(args, data):
    output = args.output or color_correction_path(args.config_name)
    legacy_output = legacy_color_correction_path(args.config_name)
    output_dir = os.path.dirname(output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    if os.path.exists(legacy_output):
        os.remove(legacy_output)
    record_usb_capture_guard_stat(args.config_name, 'auto_calibration_success')
    write_usb_capture_guard_event(
        args.config_name,
        'auto_calibration_updated',
        'USB color calibration file updated',
        output=output,
        model=args.model,
        captures=data.get('captures'),
        assets_visible=data.get('assets', {}).get('visible'),
        assets_before_pass=data.get('assets', {}).get('before_pass'),
        assets_after_pass=data.get('assets', {}).get('after_pass'),
        lut_strength=data.get('assets', {}).get('lut_strength'),
        metrics_before=data.get('metrics', {}).get('before'),
        metrics_after=data.get('metrics', {}).get('after'),
        safety=data.get('safety'),
    )
    print(f'Wrote calibration: {output}')
    print('Restart USB capture service or stop/start USB preview for the calibration to take effect.')


def main():
    args = parse_args()
    if args.apply and args.dry_run:
        raise SystemExit('--apply and --dry-run are mutually exclusive')
    if not args.apply:
        args.dry_run = True

    server = server_key_from_config(args.config_name)
    assets = parse_button_assets(server, max_buttons=args.asset_max_buttons)
    if not assets:
        raise RuntimeError('No Button assets were found for retraining')
    print(f'Loaded {len(assets)} Button assets for server={server}')

    guard_pairs = load_guard_pairs(args)
    calibration_pairs = load_calibration_pairs(args)
    pairs = guard_pairs + calibration_pairs
    print(f'Loaded {len(guard_pairs)} guard sample pair(s), {len(calibration_pairs)} calibration pair(s)')
    if not pairs:
        raise RuntimeError('No usable paired screenshots were found')
    if args.apply and not guard_pairs:
        raise RuntimeError('--apply requires at least one guard sample; use usb_capture_color_calibrate.py for snapshot-only fitting')
    if args.apply and any(pair['source'] == 'guard' and pair['usb_source'] != 'usb_raw' for pair in pairs):
        raise RuntimeError('--apply requires raw USB guard samples; rerun with --allow-corrected-usb --force to override')
    if args.apply and args.allow_unqualified_guard_samples and not args.force:
        raise RuntimeError('--apply requires quality-checked guard samples; use --force to override')

    captures, usb_sets, adb_sets, weight_sets = build_captures(args, pairs, assets)
    if not usb_sets:
        raise RuntimeError('No visible assets or guard focus samples matched the paired screenshots')

    visible = sum(len(capture['samples']) for capture in captures)
    if visible < args.asset_min_samples:
        raise RuntimeError(
            f'Only {visible} visible/guard samples available; need at least {args.asset_min_samples}'
        )

    usb_all = np.concatenate(usb_sets, axis=0)
    adb_all = np.concatenate(adb_sets, axis=0)
    weights_all = np.concatenate(weight_sets, axis=0)
    correction = fit_correction(args, usb_all, adb_all, weights_all)
    correction, strength, _ = choose_asset_correction(args, captures, args.model, correction)
    evaluation = evaluate(args, captures, correction, usb_all, adb_all)
    data = build_output_data(args, captures, assets, correction, evaluation, strength)

    failures = evaluation['safety_failures']
    if failures:
        print('Safety check failed:')
        for failure in failures:
            print(f'  - {failure}')
    else:
        print('Safety check passed.')

    if args.dry_run:
        print('Dry run only; no calibration was written.')
        return
    if failures and not args.force:
        print('Calibration not written. Use --force only if you really want to apply it.')
        write_usb_capture_guard_event(
            args.config_name,
            'auto_calibration_blocked',
            'Safety check failed; calibration was not written',
            failures=len(failures),
            failure_details=failures,
            model=args.model,
            pairs=len(pairs),
            guard_pairs=len(guard_pairs),
            calibration_pairs=len(calibration_pairs),
            visible_samples=visible,
            assets_before_pass=evaluation.get('total_before_pass'),
            assets_after_pass=evaluation.get('total_after_pass'),
        )
        raise SystemExit(2)

    if failures and args.force:
        data['safety']['forced'] = True
    write_calibration(args, data)


if __name__ == '__main__':
    main()
