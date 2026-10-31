"""Durable template icons, private source screenshots and structured statistics."""

import base64
import hashlib
import re
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from module.cloud.catalog import TemplateCatalog, map_items
from module.cloud.files import config_transaction, read_json, write_json
from module.cloud.protocol import CloudError, ITEM_ID, PublicAPI
from module.logger import logger


class CloudService:
    MAX_JOBS = 10000
    MAX_BYTES = 64 << 20

    def __init__(self, root, device_id, *, api=None, public_key=None, apply_mappings=None,
                 enabled=lambda: True, scene_enabled=lambda scene: True):
        self.root = Path(root)
        self.api = api or PublicAPI(device_id)
        self.device_id = device_id
        self.enabled = enabled
        self.scene_enabled = scene_enabled
        self.history = apply_mappings or (lambda mappings: None)
        self._pump_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._next_sync = self._next_attempt = 0
        self._failures = 0
        self._lock_path = self.root / 'state.json'
        options = {'public_key': public_key} if public_key else {}
        self.catalog = TemplateCatalog(self.root / 'catalog', self.api,
                                       apply_mappings=self._apply_history, **options)

    def _bindings(self):
        return read_json(self.root / 'bindings.json', {})

    def mappings(self, published=None):
        published = self.catalog.mappings if published is None else published
        by_id = {row['unknown_id']: row for row in published}
        rows = list(published)
        for source, binding in self._bindings().items():
            canonical = by_id.get(binding['identity_id'], {})
            rows.append({'unknown_id': source,
                         'item_id': canonical.get('item_id', binding['template_id']),
                         'tag': canonical.get('tag') or binding.get('tag', '')})
        return rows

    def _apply_history(self, published):
        self.history(self.mappings(published))

    def _capacity(self):
        files = [path for folder in ('jobs', 'icons', 'screenshots')
                 for path in (self.root / folder).glob('*.json')]
        if len(files) >= self.MAX_JOBS or sum(path.stat().st_size for path in files) >= self.MAX_BYTES:
            raise CloudError('Cloud queue is full; local statistics are retained', retry_after=300)

    def identify(self, image, local_name='', *, scene='research'):
        """Runs offline; registration completes before dependent statistics and screenshots."""
        icon = np.ascontiguousarray(image[21:70, 40:89, :3], dtype=np.uint8)
        if icon.shape != (49, 49, 3):
            raise ValueError('Expected a 96px reward item')
        for kind in ('formal', 'temporary'):
            entry = self.catalog.match(icon, kind=kind)
            if entry:
                return {'item': entry['template_id'], 'source_template_id': entry['identity_id'],
                        'tag': entry.get('tag', '')}
            # Existing local formal templates retain priority over temporary matches.
            if kind == 'formal' and local_name and local_name != 'DefaultItem' and not local_name.isdigit():
                return {'item': local_name, 'source_template_id': local_name, 'tag': ''}
        source = 'U?' + hashlib.sha256(icon.tobytes()).hexdigest()
        binding = self._bindings().get(source)
        if binding:
            item = map_items([{'item': source, 'amount': 1}], self.mappings())[0]
            return {**item, 'source_template_id': source}
        if not self.enabled() or not self.scene_enabled(scene):
            return {'item': source, 'source_template_id': source, 'tag': ''}
        with config_transaction(self._lock_path):
            path = self.root / 'icons' / (source[2:] + '.json')
            if not path.exists():
                try:
                    self._capacity()
                except CloudError as error:
                    logger.warning(f'[云端统计] 图标队列已满，保留本地待编号身份: {error}')
                    return {'item': source, 'source_template_id': source, 'tag': ''}
                ok, png = cv2.imencode('.png', cv2.cvtColor(icon, cv2.COLOR_RGB2BGR))
                if not ok:
                    raise CloudError('Could not encode template icon')
                write_json(path, {'source': source, 'image_base64': base64.b64encode(png).decode(),
                                  'scene': scene})
            else:
                # 一个未知图标可能同时被科研和委托引用，任一允许的来源均可登记。
                job = read_json(path)
                scenes = job.get('scenes', [job['scene']])
                if scene not in scenes:
                    job['scenes'] = [*scenes, scene]
                    write_json(path, job)
        self.start()
        return {'item': source, 'source_template_id': source, 'tag': ''}

    def capture(self, image, observations, *, scene):
        """Keep one lossless full frame for all its unresolved items, including cached U-IDs."""
        if not observations or not self.enabled() or not self.scene_enabled(scene):
            return
        if image.shape != (720, 1280, 3) or image.dtype != np.uint8:
            raise ValueError('Expected a complete 1280x720 RGB reward screenshot')
        ok, png = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not ok:
            raise CloudError('Could not encode source screenshot')
        raw = png.tobytes()
        digest = hashlib.sha256(raw).hexdigest()
        with config_transaction(self._lock_path):
            receipts = read_json(self.root / 'uploaded-screenshots.json', {})
            sent = receipts.get(digest, [])
            pending = [row for row in observations if row not in sent]
            if not pending:
                return
            path = self.root / 'screenshots' / (digest + '.json')
            job = read_json(path, {'image_base64': base64.b64encode(raw).decode(),
                                   'scene': scene, 'observations': []})
            self._capacity()
            for row in pending:
                if row not in job['observations']:
                    job['observations'].append(row)
            write_json(path, job)
        self.start()

    def _upload_screenshots(self):
        processed = 0
        for path in sorted((self.root / 'screenshots').glob('*.json')):
            if not self.enabled():
                return
            job = read_json(path)
            if not self.scene_enabled(job['scene']):
                continue
            processed += 1
            bindings = self._bindings()
            references = []
            for row in job['observations']:
                identity = row['source']
                if identity.startswith('U?'):
                    identity = bindings.get(identity, {}).get('identity_id', identity)
                if identity.startswith('U?'):
                    break
                references.append({'identity_id': identity, 'bounds': row['bounds']})
            else:
                response = self.api.upload_screenshot(job['image_base64'], references, job['scene'])
                if response.get('stored') is not True or response.get('sha256') != path.stem:
                    raise CloudError('Invalid source screenshot acknowledgement')
                with config_transaction(self._lock_path):
                    receipts = read_json(self.root / 'uploaded-screenshots.json', {})
                    confirmed = receipts.setdefault(path.stem, [])
                    for row in job['observations']:
                        if row not in confirmed:
                            confirmed.append(row)
                    while len(receipts) > self.MAX_JOBS:
                        del receipts[next(iter(receipts))]
                    write_json(self.root / 'uploaded-screenshots.json', receipts)
                    current = read_json(path)
                    current['observations'] = [row for row in current['observations']
                                               if row not in confirmed]
                    if current['observations']:
                        write_json(path, current)
                    else:
                        path.unlink()
            if processed >= 10:
                break

    def enqueue(self, record):
        if record.get('scene') not in ('commission', 'research'):
            raise ValueError('Only commission and research can be submitted')
        if not self.enabled() or not self.scene_enabled(record['scene']):
            return None
        record = dict(record)
        record.setdefault('record_id', uuid.uuid4().hex)
        record.setdefault('client', 'AzurPilot')
        record.setdefault('device_id', self.device_id)
        record.setdefault('template_revision', self.catalog.revision)
        # Round-trip enforces plain JSON and detaches mutable caller-owned items.
        source = record.pop('source_items', record['items'])
        with config_transaction(self._lock_path):
            path = self.root / 'jobs' / (record['record_id'] + '.json')
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', record['record_id']):
                raise ValueError('Invalid record ID')
            if not path.exists():
                self._capacity()
                write_json(path, {'record': record, 'source_items': source})
            # Registration can finish while a game collector is still committing
            # its local row. Re-apply current mappings after that row exists.
            write_json(self.root / 'history-dirty.json', uuid.uuid4().hex)
        self.start()
        return record['record_id']

    def _quarantine(self, path, reason):
        job = read_json(path)
        job['error'] = reason
        write_json(self.root / 'rejected' / path.name, job)
        path.unlink()
        logger.warning(f'[云端统计] 记录保留在 rejected: {reason}')

    def _register(self):
        processed = 0
        for path in sorted((self.root / 'icons').glob('*.json')):
            if not self.enabled():
                return
            job = read_json(path)
            scene = next((scene for scene in job.get('scenes', [job['scene']])
                          if self.scene_enabled(scene)), None)
            if scene is None:
                continue
            response = self.api.register(job['image_base64'], self.catalog.revision, scene)
            canonical, identity = response.get('template_id'), response.get('identity_id')
            if (response.get('match') not in ('new', 'cluster', 'formal')
                    or not isinstance(canonical, str) or not ITEM_ID.fullmatch(canonical)
                    or not isinstance(identity, str) or not ITEM_ID.fullmatch(identity)):
                raise CloudError('Invalid template registration acknowledgement')
            with config_transaction(self._lock_path):
                bindings = self._bindings()
                bindings[job['source']] = {'template_id': canonical, 'identity_id': identity,
                                            'tag': response.get('tag', '')}
                write_json(self.root / 'bindings.json', bindings)
            # Crash before deleting the job causes an idempotent re-registration, not a lost ID.
            self.history(self.mappings())
            path.unlink()
            self._next_sync = 0
            processed += 1
            if processed >= 50:
                break

    def _submit(self):
        paths, records = [], []
        bindings = self._bindings()
        for path in sorted((self.root / 'jobs').glob('*.json')):
            job = read_json(path)
            record = job['record']
            if not self.scene_enabled(record['scene']):
                continue
            sources = []
            for item in job['source_items']:
                identity = item.get('source_template_id', item['item'])
                if identity.startswith('U?'):
                    identity = bindings.get(identity, {}).get('identity_id', identity)
                # Preserve provenance for records collected after identities
                # were merged; the main service projects them using its mapping.
                sources.append({'item': identity, 'amount': item['amount'], 'tag': item.get('tag') or ''})
            record['items'] = map_items(sources, [])
            if any(row['item'].startswith('U?') for row in record['items']):
                continue
            # Missing scene metadata is retained for repair rather than fabricated.
            if (record.get('status') not in (0, 1)
                    or (record['scene'] == 'commission' and not record.get('comm'))
                    or (record['scene'] == 'research' and not (
                        record.get('project') and 1 <= record.get('series', 0) <= 100))):
                self._quarantine(path, 'Missing recognized scene metadata')
                continue
            record['template_revision'] = self.catalog.revision
            paths.append(path)
            records.append(record)
            if len(records) >= 200:
                break
        if not records or not self.enabled():
            return
        response = self.api.submit(records)
        if response.get('schema_version') != 3 or not isinstance(response.get('results'), list):
            raise CloudError('Invalid drop acknowledgement')
        results = response['results']
        ids = [row.get('record_id') for row in results if isinstance(row, dict)]
        if len(ids) != len(results) or len(ids) != len(set(ids)):
            raise CloudError('Duplicate or malformed drop acknowledgement')
        by_id = {row['record_id']: row for row in results}
        retry = False
        for path, record in zip(paths, records):
            ack = by_id.get(record['record_id'], {})
            if ack.get('status') in ('accepted', 'duplicate'):
                path.unlink()
            elif ack.get('status') in ('invalid', 'unsupported_scene'):
                self._quarantine(path, str(ack.get('error') or ack['status'])[:300])
            else:
                retry = True
        if retry:
            raise CloudError('Some records were not acknowledged')

    def _refresh_history(self):
        path = self.root / 'history-dirty.json'
        token = read_json(path)
        if token is None:
            return
        self.history(self.mappings())
        with config_transaction(self._lock_path):
            # A concurrent local commit must retain its own refresh request.
            if read_json(path) == token:
                path.unlink()

    def drain_once(self):
        if not self.enabled() or time.monotonic() < self._next_attempt:
            return False
        if not self._pump_lock.acquire(blocking=False):
            return False
        try:
            # A separate lock serializes workers across processes without blocking recognition writes.
            with config_transaction(self.root / 'worker.json'):
                self.catalog.refresh()
                errors = []
                try:
                    self._register()
                except Exception as error:
                    errors.append(error)
                if time.monotonic() >= self._next_sync:
                    try:
                        self.catalog.sync()
                        self._next_sync = time.monotonic() + 300
                    except Exception as error:
                        errors.append(error)
                try:
                    self._upload_screenshots()
                except Exception as error:
                    errors.append(error)
                self._refresh_history()
                self._submit()
                if errors:
                    raise errors[0]
            self._failures = 0
            return True
        except Exception as error:
            self._failures += 1
            delay = max(getattr(error, 'retry_after', 30), min(300, 2 ** min(self._failures, 8)))
            self._next_attempt = time.monotonic() + delay
            reason = str(error) if isinstance(error, CloudError) else type(error).__name__
            logger.warning(f'[云端统计] 后台同步暂缓，数据保留: {reason}')
            return False
        finally:
            self._pump_lock.release()

    def start(self):
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name='CloudStatistics', daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.drain_once()
            self._wake.wait(30)

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
