"""
Calibrate USB capture colors against an ADB screencap.

The script captures the same screen from ADB and the USB capture path, fits a
linear RGB transform, and writes:

    config/usb_color/<config-name>.json

Run while the game is on a mostly static, colorful screen. Use --captures to
fit one transform from multiple manually selected screens.

Examples:
    toolkit\\python.exe dev_tools\\usb_capture_color_calibrate.py --config-name "alas (1)"
    toolkit\\python.exe dev_tools\\usb_capture_color_calibrate.py --generate-chart
    toolkit\\python.exe dev_tools\\usb_capture_color_calibrate.py --config-name "alas (1)" --frame-fit --captures 8
"""

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cv2
import numpy as np

from dev_tools.usb_capture_preview import (
    DEFAULT_OUTPUT_SIZE,
    load_alas_config,
    open_capture,
)
from dev_tools.usb_capture_service import (
    CaptureService,
    color_correction_path,
    get_frame,
    legacy_color_correction_path,
    ping_service,
)


def parse_args():
    parser = argparse.ArgumentParser(description='Calibrate USB capture colors using ADB screencap as reference.')
    parser.add_argument('--config-name', default=os.environ.get('ALAS_CONFIG_NAME', 'alas'))
    parser.add_argument('--frame-fit', action='store_true', help='Use legacy full-frame RGB matrix fitting instead of patch profiling.')
    parser.add_argument('--generate-chart', action='store_true', help='Generate the patch profiling chart(s) and exit.')
    parser.add_argument('--chart-output', default='dev_tools/usb_capture_patch_chart.png', help='Patch chart output path.')
    parser.add_argument('--chart-page', type=int, default=1, help='Patch chart page number. Default: 1')
    parser.add_argument('--chart-pages', type=int, default=1, help='Number of patch chart pages to generate. Default: 1')
    parser.add_argument('--model', choices=('lut3d', 'channel_lut'), default='lut3d', help='Patch calibration model. Default: lut3d')
    parser.add_argument('--lut3d-size', type=int, default=33, help='3D LUT grid size. Default: 33')
    parser.add_argument('--lut3d-power', type=float, default=2.0, help='3D LUT inverse-distance power. Default: 2.0')
    parser.add_argument('--samples', type=int, default=200000, help='Maximum sampled pixels per capture. Default: 200000')
    parser.add_argument('--captures', type=int, default=1, help='Number of ADB/USB screen pairs to capture. Default: 1')
    parser.add_argument('--capture-delay', type=float, default=0.0, help='Seconds to wait before each capture after pressing Enter.')
    parser.add_argument('--warmup', type=int, default=5, help='USB frames to discard before capture. Default: 5')
    parser.add_argument('--no-service', action='store_true', help='Open USB capture directly instead of using service.')
    parser.add_argument('--output', default=None, help='Calibration JSON path. Default: config/usb_color/<config-name>.json')
    parser.add_argument('--snapshot-dir', default='log/usb_color_calibration')
    parser.add_argument('--disable', action='store_true', help='Write calibration file with enabled=false.')
    parser.add_argument('--force', action='store_true', help='Write enabled=true even if safety checks fail.')
    parser.add_argument('--max-bias', type=float, default=12.0, help='Reject calibration if RGB bias exceeds this value. Default: 12')
    parser.add_argument('--max-worse-ratio', type=float, default=1.05, help='Reject if any full-frame MAE worsens by this ratio. Default: 1.05')
    parser.add_argument('--max-worse-delta', type=float, default=1.0, help='Reject if any full-frame MAE worsens by this delta. Default: 1.0')
    parser.add_argument('--protected-color-delta', type=float, default=32.0, help='Reject if key UI colors drift beyond this max-channel delta. Default: 32')
    return parser.parse_args()


def capture_adb(config_name):
    print('Capturing ADB screenshot...')
    config = load_alas_config(os.path.join('config', f'{config_name}.json'))
    serial = str(config.get('Serial', 'auto')).strip()
    if serial == 'auto':
        raise RuntimeError('Alas.Emulator.Serial is "auto"; set an explicit serial before calibration')

    adb = find_adb()
    if ':' in serial:
        subprocess_run([adb, 'connect', serial], timeout=10, check=False)
    result = subprocess_run([adb, '-s', serial, 'exec-out', 'screencap', '-p'], timeout=10)
    data = np.frombuffer(result, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError('Failed to decode ADB PNG screencap')
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return ensure_output_size(image)


def find_adb():
    candidates = [
        os.path.join('toolkit', 'adb.exe'),
        os.path.join('bin', 'adb', 'adb.exe'),
        os.path.join(REPO_ROOT, 'toolkit', 'adb.exe'),
        os.path.join(REPO_ROOT, 'bin', 'adb', 'adb.exe'),
        'adb',
    ]
    for adb in candidates:
        if adb == 'adb' or os.path.exists(adb):
            return adb
    return 'adb'


def subprocess_run(cmd, timeout=10, check=True):
    import subprocess

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and proc.returncode:
        stderr = proc.stderr.decode('utf-8', errors='ignore').strip()
        stdout = proc.stdout.decode('utf-8', errors='ignore').strip()
        raise RuntimeError(f'Command failed: {cmd}, stdout={stdout}, stderr={stderr}')
    return proc.stdout


def capture_usb_from_service(config_name):
    print('Capturing raw USB frame from running USB capture service...')
    return get_frame(config_name, raw=True)


def capture_usb_direct(config_name, warmup):
    print('Opening USB capture directly...')
    config_path = os.path.join('config', f'{config_name}.json')
    config = load_alas_config(config_path)
    device = config.get('UsbCaptureDevice', 0)
    backend = config.get('UsbCaptureBackend', 'auto')
    codec = config.get('UsbCaptureCodec', 'MJPG')
    width = int(config.get('UsbCaptureWidth', 1280))
    height = int(config.get('UsbCaptureHeight', 720))
    fps = int(config.get('UsbCaptureFps', 30))

    cap = open_capture(device, backend, codec, width, height, fps, buffer_size=1)
    converter = CaptureService(config_name=config_name)
    try:
        frame = None
        for _ in range(max(1, warmup + 1)):
            ok, frame = cap.read()
            if not (ok and frame is not None and frame.size):
                raise RuntimeError('Unable to read USB capture frame')
            time.sleep(0.03)
        return converter.convert_frame(frame)
    finally:
        cap.release()


def ensure_output_size(image):
    width, height = DEFAULT_OUTPUT_SIZE
    if image.shape[1] == width and image.shape[0] == height:
        return np.ascontiguousarray(image[:, :, :3])
    return np.ascontiguousarray(cv2.resize(image[:, :, :3], (width, height), interpolation=cv2.INTER_AREA))


PATCH_GRID_COLS = 16
PATCH_GRID_ROWS = 8
PATCH_SIZE = 48
PATCH_AREA = (80, 74, 1200, 634)
MARKER_SIZE = 40
MARKERS = [
    {'name': 'top_left', 'color': (255, 0, 0), 'center': (32, 32), 'corner': (0, 0)},
    {'name': 'top_right', 'color': (0, 255, 0), 'center': (1248, 32), 'corner': (1279, 0)},
    {'name': 'bottom_left', 'color': (0, 0, 255), 'center': (32, 688), 'corner': (0, 719)},
    {'name': 'bottom_right', 'color': (255, 255, 0), 'center': (1248, 688), 'corner': (1279, 719)},
]
PROTECTED_COLORS = [
    (156, 255, 82),   # Opsi strategic-search green checkbox
]
MEASURED_UI_COLORS = [
    (233, 241, 127),  # Reward/EXP yellow text used by color checks.
]


def patch_colors(page=1):
    page = max(1, int(page))
    capacity = PATCH_GRID_COLS * PATCH_GRID_ROWS

    colors = []
    if page == 1:
        levels = [0, 64, 128, 192, 255]
        colors.extend((r, g, b) for r in levels for g in levels for b in levels)
        colors.extend(PROTECTED_COLORS)
        colors.extend(MEASURED_UI_COLORS)
        colors.append((74, 142, 207))  # Common blue confirmation/button tone.
    else:
        rng = np.random.default_rng(20260508 + page)
        colors.extend([(0, 0, 0), (255, 255, 255)])
        for level in range(16, 256, 16):
            colors.append((level, level, level))
        colors.extend(PROTECTED_COLORS)
        colors.extend(MEASURED_UI_COLORS)
        colors.extend([
            (74, 142, 207),
            (24, 126, 250),
            (32, 196, 64),
            (245, 208, 64),
            (220, 72, 72),
        ])
        random_colors = rng.integers(0, 256, size=(capacity * 2, 3), dtype=np.uint8)
        colors.extend(tuple(int(v) for v in color) for color in random_colors)

    # Preserve order while removing duplicates.
    unique = []
    seen = set()
    for color in colors:
        color = tuple(int(v) for v in color)
        if color not in seen:
            unique.append(color)
            seen.add(color)
        if len(unique) >= capacity:
            break
    return unique


def patch_chart_spec(page=1):
    x0, y0, x1, y1 = PATCH_AREA
    cell_w = (x1 - x0) / PATCH_GRID_COLS
    cell_h = (y1 - y0) / PATCH_GRID_ROWS
    patches = []
    for index, color in enumerate(patch_colors(page=page)):
        row, col = divmod(index, PATCH_GRID_COLS)
        cx = x0 + (col + 0.5) * cell_w
        cy = y0 + (row + 0.5) * cell_h
        half = PATCH_SIZE / 2
        patches.append({
            'index': index + 1,
            'color': tuple(int(v) for v in color),
            'rect': (cx - half, cy - half, cx + half, cy + half),
        })
    return patches


def generate_patch_chart(path, page=1):
    width, height = DEFAULT_OUTPUT_SIZE
    image = np.full((height, width, 3), 18, dtype=np.uint8)

    for marker in MARKERS:
        cx, cy = marker['center']
        half = MARKER_SIZE // 2
        cv2.rectangle(image, (cx - half - 4, cy - half - 4), (cx + half + 4, cy + half + 4), (255, 255, 255), -1)
        cv2.rectangle(image, (cx - half, cy - half), (cx + half, cy + half), marker['color'], -1)

    for patch in patch_chart_spec(page=page):
        x0, y0, x1, y1 = [int(round(v)) for v in patch['rect']]
        cv2.rectangle(image, (x0 - 2, y0 - 2), (x1 + 2, y1 + 2), (0, 0, 0), -1)
        cv2.rectangle(image, (x0, y0), (x1, y1), patch['color'], -1)

    save_image(path, image)
    print(f'Wrote patch chart page {page}: {path}')


def detect_marker_center(image, marker):
    target = np.asarray(marker['color'], dtype=np.int16)
    diff = np.max(np.abs(image.astype(np.int16) - target), axis=2)
    mask = (diff <= 18).astype(np.uint8)
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        raise RuntimeError(f'Unable to find chart marker: {marker["name"]}')

    corner = np.asarray(marker['corner'], dtype=np.float32)
    best = None
    for label in range(1, count):
        area = stats[label, cv2.CC_STAT_AREA]
        if area < 80:
            continue
        center = centers[label].astype(np.float32)
        distance = float(np.linalg.norm(center - corner))
        if best is None or distance < best[0]:
            best = (distance, center)
    if best is None:
        raise RuntimeError(f'Unable to find chart marker: {marker["name"]}')
    return best[1]


def detect_chart_transform(adb):
    detected = {marker['name']: detect_marker_center(adb, marker) for marker in MARKERS}
    left = (detected['top_left'][0] + detected['bottom_left'][0]) / 2
    right = (detected['top_right'][0] + detected['bottom_right'][0]) / 2
    top = (detected['top_left'][1] + detected['top_right'][1]) / 2
    bottom = (detected['bottom_left'][1] + detected['bottom_right'][1]) / 2

    marker_left = (MARKERS[0]['center'][0] + MARKERS[2]['center'][0]) / 2
    marker_right = (MARKERS[1]['center'][0] + MARKERS[3]['center'][0]) / 2
    marker_top = (MARKERS[0]['center'][1] + MARKERS[1]['center'][1]) / 2
    marker_bottom = (MARKERS[2]['center'][1] + MARKERS[3]['center'][1]) / 2

    scale_x = (right - left) / (marker_right - marker_left)
    scale_y = (bottom - top) / (marker_bottom - marker_top)
    if scale_x <= 0.25 or scale_y <= 0.25:
        raise RuntimeError(f'Invalid chart transform: scale_x={scale_x}, scale_y={scale_y}')
    offset_x = left - marker_left * scale_x
    offset_y = top - marker_top * scale_y
    return {'scale_x': float(scale_x), 'scale_y': float(scale_y), 'offset_x': float(offset_x), 'offset_y': float(offset_y)}


def transform_rect(rect, transform, shrink=0.45):
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    half_w = (x1 - x0) * shrink / 2
    half_h = (y1 - y0) * shrink / 2
    x0, x1 = cx - half_w, cx + half_w
    y0, y1 = cy - half_h, cy + half_h
    sx = transform['scale_x']
    sy = transform['scale_y']
    ox = transform['offset_x']
    oy = transform['offset_y']
    width, height = DEFAULT_OUTPUT_SIZE
    rx0 = max(0, min(width - 1, int(round(x0 * sx + ox))))
    rx1 = max(0, min(width, int(round(x1 * sx + ox))))
    ry0 = max(0, min(height - 1, int(round(y0 * sy + oy))))
    ry1 = max(0, min(height, int(round(y1 * sy + oy))))
    if rx1 <= rx0 or ry1 <= ry0:
        raise RuntimeError('Invalid transformed patch rectangle')
    return rx0, ry0, rx1, ry1


def mean_rect(image, rect):
    x0, y0, x1, y1 = rect
    return image[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)


def sample_chart_patches(adb, usb, page=1):
    transform = detect_chart_transform(adb)
    adb_values = []
    usb_values = []
    patch_results = []
    for patch in patch_chart_spec(page=page):
        rect = transform_rect(patch['rect'], transform)
        adb_mean = mean_rect(adb, rect)
        usb_mean = mean_rect(usb, rect)
        adb_values.append(adb_mean)
        usb_values.append(usb_mean)
        patch_results.append({
            'index': patch['index'],
            'target_color': list(patch['color']),
            'rect': list(rect),
            'adb': adb_mean.round(3).tolist(),
            'usb': usb_mean.round(3).tolist(),
        })
    return np.asarray(usb_values, dtype=np.float32), np.asarray(adb_values, dtype=np.float32), transform, patch_results


def fit_channel_luts(usb_values, adb_values):
    luts = []
    query = np.arange(256, dtype=np.float32)
    for channel in range(3):
        x = usb_values[:, channel]
        y = adb_values[:, channel]
        # Strong neutral anchors keep the LUT from inventing black lift or
        # white compression between sparse patch samples.
        low_anchors = np.arange(0, 33, dtype=np.float32)
        high_anchors = np.arange(224, 256, dtype=np.float32)
        anchors = np.concatenate([
            low_anchors,
            np.array([48, 64, 96, 128, 160, 192], dtype=np.float32),
            high_anchors,
        ])
        x = np.concatenate([x, np.repeat(anchors, 12)])
        y = np.concatenate([y, np.repeat(anchors, 12)])

        bins = {}
        for xi, yi in zip(x, y):
            key = int(round(float(xi)))
            bins.setdefault(key, []).append(float(yi))
        xs = np.asarray(sorted(bins), dtype=np.float32)
        ys = np.asarray([np.mean(bins[int(v)]) for v in xs], dtype=np.float32)
        lut = np.interp(query, xs, ys)
        lut = np.maximum.accumulate(lut)
        # Clamp the extreme shadows/highlights to a gentle identity blend.
        lut[:17] = query[:17]
        lut[239:] = query[239:]
        luts.append(np.clip(np.rint(lut), 0, 255).astype(np.uint8))
    return np.stack(luts, axis=0)


def apply_channel_luts(image, luts):
    corrected = np.empty_like(image)
    for channel in range(3):
        corrected[:, :, channel] = luts[channel][image[:, :, channel]]
    return corrected


def transform_color_lut(color, luts):
    color = np.asarray(color, dtype=np.uint8)
    return np.asarray([luts[channel][color[channel]] for channel in range(3)], dtype=np.float32)


def fit_lut3d(usb_values, adb_values, size=17, power=2.0):
    size = max(5, min(33, int(size)))
    grid_axis = np.linspace(0, 255, size, dtype=np.float32)
    rr, gg, bb = np.meshgrid(grid_axis, grid_axis, grid_axis, indexing='ij')
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3)

    points = usb_values.astype(np.float32)
    targets = adb_values.astype(np.float32)
    weights = np.ones((points.shape[0],), dtype=np.float32)

    # Keep the very dark and very bright neutral axis stable. This avoids
    # fixing yellow by turning black UI masks into gray.
    neutral = np.asarray([(v, v, v) for v in list(range(0, 25)) + list(range(232, 256))], dtype=np.float32)
    if neutral.size:
        points = np.concatenate([points, neutral], axis=0)
        targets = np.concatenate([targets, neutral], axis=0)
        weights = np.concatenate([weights, np.full((neutral.shape[0],), 24.0, dtype=np.float32)], axis=0)

    # A few identity color anchors prevent wild extrapolation in empty corners
    # of RGB space while still allowing measured patch colors to dominate.
    anchors = np.asarray([
        (0, 0, 0), (255, 255, 255),
        (255, 0, 0), (0, 255, 0), (0, 0, 255),
        (255, 255, 0), (255, 0, 255), (0, 255, 255),
    ], dtype=np.float32)
    points = np.concatenate([points, anchors], axis=0)
    targets = np.concatenate([targets, anchors], axis=0)
    weights = np.concatenate([weights, np.full((anchors.shape[0],), 4.0, dtype=np.float32)], axis=0)

    residuals = targets - points
    lut = np.empty_like(grid)
    chunk_size = 512
    for start in range(0, grid.shape[0], chunk_size):
        chunk = grid[start:start + chunk_size]
        diff = chunk[:, None, :] - points[None, :, :]
        dist2 = np.sum(diff * diff, axis=2)
        inv = weights[None, :] / np.power(dist2 + 1e-3, power / 2)
        weighted = inv @ residuals
        denom = np.sum(inv, axis=1)[:, None]
        lut[start:start + chunk.shape[0]] = chunk + weighted / denom

    lut = np.clip(np.rint(lut), 0, 255).astype(np.uint8).reshape(size, size, size, 3)
    lut[0, 0, 0] = (0, 0, 0)
    lut[-1, -1, -1] = (255, 255, 255)
    return lut


def apply_lut3d(image, lut):
    size = lut.shape[0]
    scaled = image.astype(np.float32) * ((size - 1) / 255.0)
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


def transform_color_lut3d(color, lut):
    image = np.asarray(color, dtype=np.uint8).reshape(1, 1, 3)
    return apply_lut3d(image, lut).reshape(3).astype(np.float32)


def apply_patch_model(image, model, correction):
    if model == 'channel_lut':
        return apply_channel_luts(image, correction)
    return apply_lut3d(image, correction)


def transform_patch_color(color, model, correction):
    if model == 'channel_lut':
        return transform_color_lut(color, correction)
    return transform_color_lut3d(color, correction)


def color_tolerance(color1, color2):
    diff = np.asarray(color1, dtype=np.float32) - np.asarray(color2, dtype=np.float32)
    max_positive = max(0.0, float(np.max(diff)))
    max_negative = min(0.0, float(np.min(diff)))
    return max_positive - max_negative


def patch_safety_check(args, model, correction, before, after, capture_results):
    failures = []
    if after['mae'] >= before['mae']:
        failures.append(f'patch MAE did not improve: {before["mae"]:.2f} -> {after["mae"]:.2f}')

    black = transform_patch_color((0, 0, 0), model, correction)
    white = transform_patch_color((255, 255, 255), model, correction)
    if float(np.max(black)) > 4:
        failures.append(f'black anchor lifted to {black.round(1).tolist()}')
    if float(np.min(white)) < 240:
        failures.append(f'white anchor compressed to {white.round(1).tolist()}')

    for color in PROTECTED_COLORS:
        corrected = transform_patch_color(color, model, correction)
        delta = color_tolerance(corrected, color)
        if delta > args.protected_color_delta:
            failures.append(
                f'protected color {color} drifts by {delta:.1f}: {corrected.round(1).tolist()}'
            )

    key_colors = set(PROTECTED_COLORS + MEASURED_UI_COLORS)
    for item in capture_results:
        for patch in item.get('patch_results', []):
            target = tuple(int(v) for v in patch['target_color'])
            if target not in key_colors:
                continue
            corrected = transform_patch_color(patch['usb'], model, correction)
            delta = color_tolerance(corrected, target)
            if delta > 10:
                failures.append(
                    f'key patch {target} on capture {item["index"]} remains off by {delta:.1f}: '
                    f'usb={np.asarray(patch["usb"]).round(1).tolist()}, corrected={corrected.round(1).tolist()}'
                )

    for item in capture_results:
        before_mae = item['metrics']['before']['mae']
        after_mae = item['metrics']['after']['mae']
        if after_mae > before_mae * args.max_worse_ratio and after_mae - before_mae > args.max_worse_delta:
            failures.append(f'capture {item["index"]} worsened: {before_mae:.2f} -> {after_mae:.2f}')
    return failures


def sample_pixels(adb, usb, max_samples, seed=20260508):
    adb_pixels = adb.reshape(-1, 3).astype(np.float32)
    usb_pixels = usb.reshape(-1, 3).astype(np.float32)

    mask = np.ones((adb_pixels.shape[0],), dtype=bool)
    mask &= np.all(adb_pixels > 4, axis=1)
    mask &= np.all(adb_pixels < 251, axis=1)
    mask &= np.all(usb_pixels > 4, axis=1)
    mask &= np.all(usb_pixels < 251, axis=1)

    indexes = np.flatnonzero(mask)
    if indexes.size < 1000:
        indexes = np.arange(adb_pixels.shape[0])
    if indexes.size > max_samples:
        rng = np.random.default_rng(seed)
        indexes = rng.choice(indexes, size=max_samples, replace=False)

    return usb_pixels[indexes], adb_pixels[indexes]


def fit_color_matrix(usb_pixels, adb_pixels):
    x = np.concatenate([
        usb_pixels,
        np.ones((usb_pixels.shape[0], 1), dtype=np.float32),
    ], axis=1)
    coeff, _, _, _ = np.linalg.lstsq(x, adb_pixels, rcond=None)
    matrix = coeff[:3, :].astype(np.float32)
    bias = coeff[3, :].astype(np.float32)
    return matrix, bias


def apply_color_matrix(image, matrix, bias):
    corrected = image.astype(np.float32).reshape(-1, 3) @ matrix + bias
    corrected = np.clip(corrected, 0, 255).astype(np.uint8)
    return corrected.reshape(image.shape)


def metrics(name, image, target):
    diff = image.astype(np.float32) - target.astype(np.float32)
    mae = float(np.mean(np.abs(diff)))
    maxe = float(np.max(np.abs(diff)))
    print(f'{name}: MAE={mae:.2f}, max={maxe:.1f}')
    return {'mae': mae, 'max': maxe}


def metrics_pixels(name, image_pixels, target_pixels):
    diff = image_pixels.astype(np.float32) - target_pixels.astype(np.float32)
    mae = float(np.mean(np.abs(diff)))
    maxe = float(np.max(np.abs(diff)))
    print(f'{name}: MAE={mae:.2f}, max={maxe:.1f}')
    return {'mae': mae, 'max': maxe}


def save_image(path, image):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, cv2.cvtColor(image, cv2.COLOR_RGB2BGR))


def write_disabled(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as file:
        json.dump({'enabled': False}, file, indent=2, ensure_ascii=False)
    print(f'Wrote disabled calibration: {path}')


def transform_color(color, matrix, bias):
    color = np.asarray(color, dtype=np.float32)
    return np.clip(color @ matrix + bias, 0, 255)


def safety_check(args, matrix, bias, capture_results):
    failures = []

    max_bias = float(np.max(np.abs(bias)))
    if max_bias > args.max_bias:
        failures.append(f'bias too large: {max_bias:.2f} > {args.max_bias:.2f}')

    for item in capture_results:
        before = item['metrics']['before']['mae']
        after = item['metrics']['after']['mae']
        if after > before * args.max_worse_ratio and after - before > args.max_worse_delta:
            failures.append(
                f'capture {item["index"]} worsened: {before:.2f} -> {after:.2f}'
            )

    protected_colors = [
        (156, 255, 82),   # Opsi strategic-search green checkbox
        (233, 241, 127),  # Reward/EXP yellow text used by color checks
    ]
    for color in protected_colors:
        corrected = transform_color(color, matrix, bias)
        delta = float(np.max(np.abs(corrected - np.asarray(color, dtype=np.float32))))
        if delta > args.protected_color_delta:
            failures.append(
                f'protected color {color} drifts by {delta:.1f}: {corrected.round(1).tolist()}'
            )

    return failures


def wait_for_capture(args, index):
    if args.captures <= 1:
        if args.capture_delay > 0:
            time.sleep(args.capture_delay)
        return

    print()
    input(
        f'[{index + 1}/{args.captures}] '
        'Display the next calibration image fullscreen on Android, then press Enter...'
    )
    if args.capture_delay > 0:
        time.sleep(args.capture_delay)


def capture_usb(args):
    if not args.no_service and ping_service(args.config_name):
        try:
            return capture_usb_from_service(args.config_name)
        except RuntimeError as e:
            if 'Unknown command: frame_raw' not in str(e):
                raise
            print('Running USB capture service is old and does not support raw frames.')
            print('Falling back to direct USB capture. If the device is busy, stop/restart USB preview and run again.')
    return capture_usb_direct(args.config_name, warmup=args.warmup)


def run_patch_calibration(args, output, legacy_output):
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    snapshot_dir = os.path.join(args.snapshot_dir, args.config_name.replace(os.sep, '_'), timestamp)
    captures = []
    usb_patch_sets = []
    adb_patch_sets = []

    if args.captures > 1:
        print('Use one generated patch chart page per capture for best coverage.')
        print('Example: /sdcard/Pictures/usb_capture_patch_chart_page_01.png, page_02.png, ...')

    for index in range(args.captures):
        if args.captures > 1:
            page = index + 1
            print()
            input(
                f'[{index + 1}/{args.captures}] '
                f'Display usb_capture_patch_chart_page_{page:02d}.png fullscreen on Android, then press Enter...'
            )
            if args.capture_delay > 0:
                time.sleep(args.capture_delay)
        else:
            page = args.chart_page
            print('Display dev_tools/usb_capture_patch_chart.png fullscreen on Android, then press Enter...')
            input()
            if args.capture_delay > 0:
                time.sleep(args.capture_delay)

        adb = capture_adb(args.config_name)
        usb = ensure_output_size(capture_usb(args))
        usb_patches, adb_patches, transform, patch_results = sample_chart_patches(adb, usb, page=page)
        print(f'Captured patch chart page {page}: {len(patch_results)} patches')
        print(
            'Chart transform: '
            f'scale=({transform["scale_x"]:.4f}, {transform["scale_y"]:.4f}), '
            f'offset=({transform["offset_x"]:.1f}, {transform["offset_y"]:.1f})'
        )
        captures.append({
            'index': index + 1,
            'adb': adb,
            'usb': usb,
            'patch_results': patch_results,
            'transform': transform,
            'chart_page': page,
        })
        usb_patch_sets.append(usb_patches)
        adb_patch_sets.append(adb_patches)

    usb_patches_all = np.concatenate(usb_patch_sets, axis=0)
    adb_patches_all = np.concatenate(adb_patch_sets, axis=0)
    if args.model == 'channel_lut':
        print(f'Fitting per-channel LUT with {usb_patches_all.shape[0]} measured patches...')
        correction = fit_channel_luts(usb_patches_all, adb_patches_all)
    else:
        print(f'Fitting 3D LUT with {usb_patches_all.shape[0]} measured patches...')
        correction = fit_lut3d(
            usb_patches_all,
            adb_patches_all,
            size=args.lut3d_size,
            power=args.lut3d_power,
        )

    before = metrics_pixels('Patch before', usb_patches_all, adb_patches_all)
    if args.model == 'channel_lut':
        corrected_patches = np.stack([
            correction[channel][np.clip(np.rint(usb_patches_all[:, channel]), 0, 255).astype(np.uint8)]
            for channel in range(3)
        ], axis=1).astype(np.float32)
    else:
        corrected_patches = apply_lut3d(
            np.clip(np.rint(usb_patches_all), 0, 255).astype(np.uint8).reshape(-1, 1, 3),
            correction,
        ).reshape(-1, 3).astype(np.float32)
    after = metrics_pixels('Patch after ', corrected_patches, adb_patches_all)

    capture_results = []
    for capture in captures:
        index = capture['index']
        adb = capture['adb']
        usb = capture['usb']
        corrected = apply_patch_model(usb, args.model, correction)

        print(f'Capture {index} full-frame metrics:')
        full_before = metrics('  Before', usb, adb)
        full_after = metrics('  After ', corrected, adb)

        prefix = f'capture_{index:02d}'
        adb_path = os.path.join(snapshot_dir, f'{prefix}_adb.png')
        usb_path = os.path.join(snapshot_dir, f'{prefix}_usb_raw.png')
        corrected_path = os.path.join(snapshot_dir, f'{prefix}_usb_corrected.png')
        save_image(adb_path, adb)
        save_image(usb_path, usb)
        save_image(corrected_path, corrected)

        capture_results.append({
            'index': index,
            'chart_page': capture['chart_page'],
            'patches': len(capture['patch_results']),
            'transform': capture['transform'],
            'metrics': {
                'before': full_before,
                'after': full_after,
            },
            'patch_results': capture['patch_results'],
            'snapshots': {
                'adb': adb_path.replace('\\', '/'),
                'usb_raw': usb_path.replace('\\', '/'),
                'usb_corrected': corrected_path.replace('\\', '/'),
            },
        })

    data = {
        'enabled': True,
        'config_name': args.config_name,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'model': args.model,
        'captures': args.captures,
        'patches': int(usb_patches_all.shape[0]),
        'chart': {
            'size': list(DEFAULT_OUTPUT_SIZE),
            'grid': [PATCH_GRID_COLS, PATCH_GRID_ROWS],
            'patch_size': PATCH_SIZE,
            'pages': args.captures,
        },
        'metrics': {
            'before': before,
            'after': after,
        },
        'capture_results': capture_results,
    }
    if args.model == 'channel_lut':
        data['luts'] = correction.tolist()
    else:
        data['lut_size'] = int(correction.shape[0])
        data['lut'] = correction.tolist()

    safety_failures = patch_safety_check(args, args.model, correction, before, after, capture_results)
    if safety_failures:
        print('Safety check failed:')
        for failure in safety_failures:
            print(f'  - {failure}')
        data['safety'] = {'passed': False, 'failures': safety_failures}
        if not args.force:
            data['enabled'] = False
            print('Calibration was written disabled. Use --force only if you really want to apply it.')
    else:
        data['safety'] = {'passed': True, 'failures': []}

    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    if os.path.exists(legacy_output):
        os.remove(legacy_output)
    print(f'Wrote calibration: {output}')
    print('Restart USB capture service or stop/start USB preview for the calibration to take effect.')


def main():
    args = parse_args()
    args.captures = max(1, int(args.captures))
    args.chart_page = max(1, int(args.chart_page))
    args.chart_pages = max(1, int(args.chart_pages))
    output = args.output or color_correction_path(args.config_name)
    legacy_output = legacy_color_correction_path(args.config_name)
    if args.generate_chart:
        if args.chart_pages <= 1:
            generate_patch_chart(args.chart_output, page=args.chart_page)
        else:
            root, ext = os.path.splitext(args.chart_output)
            for page in range(1, args.chart_pages + 1):
                generate_patch_chart(f'{root}_page_{page:02d}{ext}', page=page)
        return
    if args.disable:
        write_disabled(output)
        if os.path.exists(legacy_output):
            os.remove(legacy_output)
        return
    if not args.frame_fit:
        run_patch_calibration(args, output, legacy_output)
        return

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    snapshot_dir = os.path.join(args.snapshot_dir, args.config_name.replace(os.sep, '_'), timestamp)
    captures = []
    usb_sample_sets = []
    adb_sample_sets = []

    for index in range(args.captures):
        wait_for_capture(args, index)
        adb = capture_adb(args.config_name)
        usb = ensure_output_size(capture_usb(args))
        usb_pixels, adb_pixels = sample_pixels(
            adb,
            usb,
            max_samples=args.samples,
            seed=20260508 + index,
        )
        print(f'Captured pair {index + 1}/{args.captures}: {usb_pixels.shape[0]} sampled pixels')

        captures.append({
            'index': index + 1,
            'adb': adb,
            'usb': usb,
            'sample_count': int(usb_pixels.shape[0]),
        })
        usb_sample_sets.append(usb_pixels)
        adb_sample_sets.append(adb_pixels)

    usb_pixels_all = np.concatenate(usb_sample_sets, axis=0)
    adb_pixels_all = np.concatenate(adb_sample_sets, axis=0)
    print(f'Fitting RGB transform with {usb_pixels_all.shape[0]} sampled pixels from {args.captures} capture(s)...')
    matrix, bias = fit_color_matrix(usb_pixels_all, adb_pixels_all)

    print('Sampled-pixel metrics:')
    before = metrics_pixels('Before', usb_pixels_all, adb_pixels_all)
    corrected_pixels_all = np.clip(usb_pixels_all @ matrix + bias, 0, 255)
    after = metrics_pixels('After ', corrected_pixels_all, adb_pixels_all)

    capture_results = []
    for capture in captures:
        index = capture['index']
        adb = capture['adb']
        usb = capture['usb']
        corrected = apply_color_matrix(usb, matrix, bias)

        print(f'Capture {index} full-frame metrics:')
        full_before = metrics('  Before', usb, adb)
        full_after = metrics('  After ', corrected, adb)

        prefix = f'capture_{index:02d}'
        adb_path = os.path.join(snapshot_dir, f'{prefix}_adb.png')
        usb_path = os.path.join(snapshot_dir, f'{prefix}_usb_raw.png')
        corrected_path = os.path.join(snapshot_dir, f'{prefix}_usb_corrected.png')
        save_image(adb_path, adb)
        save_image(usb_path, usb)
        save_image(corrected_path, corrected)

        capture_results.append({
            'index': index,
            'samples': capture['sample_count'],
            'metrics': {
                'before': full_before,
                'after': full_after,
            },
            'snapshots': {
                'adb': adb_path.replace('\\', '/'),
                'usb_raw': usb_path.replace('\\', '/'),
                'usb_corrected': corrected_path.replace('\\', '/'),
            },
        })

    data = {
        'enabled': True,
        'config_name': args.config_name,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'model': 'rgb_linear',
        'matrix': matrix.tolist(),
        'bias': bias.tolist(),
        'captures': args.captures,
        'samples_per_capture': int(args.samples),
        'samples': int(usb_pixels_all.shape[0]),
        'metrics': {
            'before': before,
            'after': after,
        },
        'capture_results': capture_results,
    }

    safety_failures = safety_check(args, matrix, bias, capture_results)
    if safety_failures:
        print('Safety check failed:')
        for failure in safety_failures:
            print(f'  - {failure}')
        data['safety'] = {
            'passed': False,
            'failures': safety_failures,
        }
        if not args.force:
            data['enabled'] = False
            print('Calibration was written disabled. Use --force only if you really want to apply it.')
    else:
        data['safety'] = {'passed': True, 'failures': []}

    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    if os.path.exists(legacy_output):
        os.remove(legacy_output)
    print(f'Wrote calibration: {output}')
    print('Restart USB capture service or stop/start USB preview for the calibration to take effect.')


if __name__ == '__main__':
    main()
