"""Bounded disk outbox for A's resumable screenshot protocol.

Screenshots are written before a background thread sends them. Immutable job IDs
and files survive retries/restarts, so A can reuse its original hash record.
"""
import hashlib
import json
import re
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import cv2
import requests

from deploy.api_endpoints import DIRECT_UPLOAD_API_URL
from module.base.device_id import get_device_id
from module.logger import logger

MAX_FILE_BYTES = 10 << 20
MAX_GROUP_BYTES = 500 << 20
MAX_SPOOL_BYTES = 512 << 20
JOB_NAME = re.compile(r'^[0-9a-f-]{36}\.(?:pending|part)$')

# 云端上传硬开关：False 时不再入队，也不发送已存在的暂存任务。
UPLOAD_ENABLED = False


class UploadDeferred(Exception):
    """Another instance in this process has already received server backoff."""


class UploadRetryLater(Exception):
    def __init__(self, response):
        self.status = response.status_code
        self.retry_after = 0.0
        raw = response.headers.get('Retry-After', '')
        try:
            value = float(raw)
            if 0 <= value < float('inf'):
                self.retry_after = value
        except (TypeError, ValueError):
            try:
                self.retry_after = max(0, parsedate_to_datetime(raw).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError, AttributeError):
                pass
        super().__init__(f'upload HTTP {self.status}; retry later')


class ServerBackoff:
    """Share server-requested pauses across instances, without pacing success."""
    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0.0
        self.failures = 0

    def remaining(self):
        with self.lock:
            return max(0.0, self.until - time.monotonic())

    def throttle(self, retry_after):
        with self.lock:
            now = time.monotonic()
            if now >= self.until:
                self.failures = min(6, self.failures + 1)
                base = max(retry_after, min(120, 5 * 2 ** (self.failures - 1)))
                self.until = now + base + random.uniform(1, min(30, max(2, base / 2)))
            else:
                # Simultaneous failures are one cooldown, not six exponential
                # escalations. A later real Retry-After may still extend it.
                self.until = max(self.until, now + retry_after)
            return max(0.0, self.until - now)

    def success(self):
        with self.lock:
            self.failures = 0


_server_backoffs = {}
_server_backoffs_lock = threading.Lock()


def server_backoff(base_url):
    endpoint = urlsplit(base_url)
    key = endpoint.scheme, endpoint.netloc.lower()
    with _server_backoffs_lock:
        return _server_backoffs.setdefault(key, ServerBackoff())


class _FileSlice:
    """A bounded file view: stream each signed part without loading it in RAM."""
    def __init__(self, file, size, queue):
        self.file = file
        self.size = size
        self.remaining = size
        self.queue = queue

    def __len__(self):
        return self.size

    def tell(self):
        return self.size - self.remaining

    def read(self, size=-1):
        if self.queue.stop.is_set() or not self.queue.enabled():
            raise ValueError('cloud upload stopped')
        length = min(self.remaining, 64 << 10, size if size >= 0 else 64 << 10)
        if not length:
            return b''
        block = self.file.read(length)
        if not block:
            raise ValueError('outbox file was truncated')
        self.remaining -= len(block)
        return block


class CloudUploadQueue:
    def __init__(self, root, *, spool_bytes=MAX_SPOOL_BYTES, base_url=DIRECT_UPLOAD_API_URL,
                 enabled=lambda: True, protocol_version=2):
        self.root = Path(root)
        self.limit = spool_bytes
        self.base_url = base_url.rstrip('/')
        self.backoff = server_backoff(self.base_url)
        self.enabled = enabled
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.next_try = {}
        if protocol_version not in (1, 2):
            raise ValueError('unsupported cloud upload protocol')
        self.protocol_version = protocol_version

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.thread = threading.Thread(target=self._run, daemon=True, name='CloudUpload')
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=95)

    def _remove(self, job):
        # Jobs are flat and owned by this outbox. Never follow a symlink or
        # recursively remove an arbitrary configured directory.
        if job.is_symlink() or job.parent.resolve() != self.root.resolve() or not JOB_NAME.fullmatch(job.name):
            raise ValueError('invalid outbox job path')
        for entry in job.iterdir():
            if entry.is_dir() or entry.is_symlink():
                raise ValueError('unexpected outbox entry')
            entry.unlink()
        job.rmdir()

    def _used(self):
        return sum(p.stat().st_size for d in self.root.iterdir()
                   if d.is_dir() and not d.is_symlink() and JOB_NAME.fullmatch(d.name)
                   for p in d.iterdir() if p.is_file() and not p.is_symlink())

    def _upgrade_legacy(self, job):
        """Send a queued v1 group straight to 123 Pan instead of through A.

        A's uplink is saturated, so a legacy group is upgraded to the direct
        protocol before its first send. It gets a fresh upload ID because A may
        still hold the older v1 attempt, and the per-file hashes are persisted
        so a retry never hashes the same screenshots twice.
        """
        metadata = json.loads((job / 'metadata.json').read_text(encoding='utf-8'))
        if metadata.get('upload_version') == 2:
            return job
        sizes = metadata.get('file_sizes')
        if not isinstance(sizes, list) or not sizes:
            raise ValueError('invalid outbox manifest')
        hashes = []
        for index in range(len(sizes)):
            md5, sha = hashlib.md5(), hashlib.sha256()
            with (job / f'{index}.png').open('rb') as file:
                for block in iter(lambda: file.read(64 << 10), b''):
                    md5.update(block)
                    sha.update(block)
            hashes.append({'md5': md5.hexdigest(), 'sha256': sha.hexdigest()})
        upload_id = str(uuid.uuid4())
        upgraded = {**metadata, 'upload_id': upload_id, 'upload_version': 2, 'files': hashes}
        payload = json.dumps(upgraded, ensure_ascii=False).encode('utf-8')
        if len(payload) > (256 << 10):
            raise ValueError('cloud metadata exceeds limit')
        staging = job / 'metadata.json.next'
        staging.write_bytes(payload)
        target = self.root / f'{upload_id}.pending'
        with self.lock:
            job.rename(target)
            (target / 'metadata.json.next').replace(target / 'metadata.json')
            self.next_try.pop(job.name, None)
        return target

    def enqueue(self, images, metadata):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            upload_id = str(uuid.uuid4())
            stage = self.root / f'{upload_id}.part'
            stage.mkdir(mode=0o700)
            try:
                used = self._used()
                if len(list(self.root.iterdir())) > 128:
                    raise ValueError('too many pending cloud upload groups')
                sizes = []
                hashes = []
                total = 0
                for index, image in enumerate(images):
                    if self.stop.is_set():
                        raise ValueError('cloud upload stopped')
                    if image.shape[:2] != (720, 1280):
                        raise ValueError('cloud screenshots must be 1280x720')
                    ok, encoded = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
                    if not ok:
                        raise ValueError('PNG encoding failed')
                    size = encoded.nbytes
                    total += size
                    if size > MAX_FILE_BYTES or total > MAX_GROUP_BYTES or used + total + 16384 > self.limit or index >= 1000:
                        raise ValueError('cloud screenshot/outbox size limit reached')
                    with (stage / f'{index}.png').open('wb') as file:
                        file.write(memoryview(encoded))
                    sizes.append(size)
                    if self.protocol_version == 2:
                        hashes.append({'md5': hashlib.md5(memoryview(encoded)).hexdigest(),
                                       'sha256': hashlib.sha256(memoryview(encoded)).hexdigest()})
                if not sizes:
                    raise ValueError('no screenshots')
                manifest = {**metadata, 'upload_id': upload_id, 'file_sizes': sizes}
                if self.protocol_version == 2:
                    manifest['upload_version'] = 2
                    manifest['files'] = hashes
                data = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
                if len(data) > (256 << 10 if self.protocol_version == 2 else 16384) or used + total + len(data) > self.limit:
                    raise ValueError('cloud metadata exceeds limit')
                (stage / 'metadata.json').write_bytes(data)
                stage.rename(self.root / f'{upload_id}.pending')
                self.wake.set()
                return upload_id
            except Exception:
                if stage.exists():
                    self._remove(stage)
                raise

    def send(self, job, session):
        metadata = json.loads((job / 'metadata.json').read_text(encoding='utf-8'))
        version = metadata.pop('upload_version', 1)
        hashes = metadata.pop('files', None)
        if version not in (1, 2):
            raise ValueError('unsupported outbox upload protocol')
        upload_id = metadata['upload_id']
        if job.name != f'{upload_id}.pending' or not JOB_NAME.fullmatch(job.name):
            raise ValueError('invalid upload manifest')
        for i, expected in enumerate(metadata['file_sizes']):
            if (job / f'{i}.png').stat().st_size != expected:
                raise ValueError('outbox file size changed')
        headers = {'X-AzurPilot-ID': metadata['device_id'], 'User-Agent': 'AzurPilot/2'}

        def base_for(version):
            parsed = urlsplit(self.base_url)
            if not re.fullmatch(r'/api/v[12]/uploads', parsed.path):
                raise ValueError('invalid cloud upload API base')
            return urlunsplit((parsed.scheme, parsed.netloc, f'/api/v{version}/uploads', '', ''))

        def accepted(response):
            if response.status_code in (429, 503):
                error = UploadRetryLater(response)
                response.close()
                raise error
            response.raise_for_status()
            body = response.json()
            if response.status_code not in (200, 202) or body.get('accepted') is not True or body.get('upload_id') != upload_id:
                raise ValueError('unexpected upload acknowledgement')
            return body

        # C only returns A's public address. Resolve it once with the small
        # manifest; send all image bytes and the completion request directly to A.
        base_url = base_for(version)
        manifest = metadata
        if version == 2:
            if not isinstance(hashes, list) or len(hashes) != len(metadata['file_sizes']):
                raise ValueError('missing direct upload hashes')
            for i, expected in enumerate(hashes):
                digest_md5, digest_sha = hashlib.md5(), hashlib.sha256()
                with (job / f'{i}.png').open('rb') as file:
                    for block in iter(lambda: file.read(64 << 10), b''):
                        digest_md5.update(block)
                        digest_sha.update(block)
                if expected != {'md5': digest_md5.hexdigest(), 'sha256': digest_sha.hexdigest()}:
                    raise ValueError('outbox file hashes changed')
            manifest = {'schema_version': 2, 'metadata': metadata, 'files': hashes}
        response = self._post(session, base_url + '/init', json=manifest, headers=headers,
                                timeout=(5, 15), allow_redirects=False)
        if response.status_code in (307, 308):
            try:
                target = urlsplit(urljoin(base_url + '/init', response.headers.get('Location', '')))
                if (target.scheme not in ('http', 'https') or not target.hostname
                        or target.username is not None or target.password is not None
                        or target.path != f'/api/v{version}/uploads/init' or target.query or target.fragment):
                    raise ValueError('invalid upload redirect')
                target.port  # Validate a numeric port before following the redirect.
                base_url = urlunsplit((target.scheme, target.netloc, f'/api/v{version}/uploads', '', ''))
                if base_url == base_for(version):
                    raise ValueError('upload redirect loop')
            finally:
                response.close()
            response = self._post(session, base_url + '/init', json=manifest, headers=headers,
                                    timeout=(5, 15), allow_redirects=False)
        if version == 2:
            original_accepted = accepted

            def accepted(response):
                body = original_accepted(response)
                if body.get('schema_version') != 2:
                    raise ValueError('unexpected direct upload protocol version')
                return body
        accepted(response)
        for i in range(len(metadata['file_sizes'])):
            if self.stop.is_set() or not self.enabled():
                raise ValueError('cloud upload stopped')
            if version == 2:
                self._send_direct_file(job / f'{i}.png', metadata['file_sizes'][i],
                                       f'{base_url}/{upload_id}/files/{i}', session, headers, accepted)
            else:
                with (job / f'{i}.png').open('rb') as file:
                    accepted(session.put(f'{base_url}/{upload_id}/files/{i}', data=file,
                                         headers={**headers, 'Content-Type': 'image/png'},
                                         timeout=(5, 85), allow_redirects=False))
        result = accepted(self._post(session, f'{base_url}/{upload_id}/complete', headers=headers,
                                       timeout=(5, 15), allow_redirects=False))
        if not re.fullmatch(r'[0-9a-f]{64}', str(result.get('sha256', ''))):
            raise ValueError('missing completed group hash')
        return result

    def _post(self, session, url, **kwargs):
        if self.backoff.remaining() > 0:
            raise UploadDeferred()
        return session.post(url, **kwargs)

    def _send_direct_file(self, path, size, api, session, headers, accepted):
        prepared = accepted(self._post(session, api + '/prepare', headers=headers,
                                         timeout=(5, 15), allow_redirects=False))
        if prepared.get('completed') is True:
            return
        if prepared.get('finalizing') is not True:
            parts = prepared.get('parts')
            if not isinstance(parts, list) or not 1 <= len(parts) <= 32:
                raise ValueError('invalid cloud upload parts')
            offset = 0
            with path.open('rb') as file:
                for number, part in enumerate(parts, 1):
                    if self.backoff.remaining() > 0:
                        raise UploadDeferred()
                    if self.stop.is_set() or not self.enabled():
                        raise ValueError('cloud upload stopped')
                    target = urlsplit(str(part.get('url', '')))
                    length = part.get('size')
                    if (target.scheme != 'https' or not target.hostname or target.username is not None
                            or target.password is not None or target.fragment
                            or part.get('number') != number or part.get('offset') != offset
                            or type(length) is not int or not 0 < length <= size - offset):
                        raise ValueError('invalid presigned upload part')
                    target.port
                    # A's headers and the API session never go to cloud storage.
                    # A fresh session also prevents cookies/auth or a redirect
                    # from changing the exact destination and signed request.
                    with requests.Session() as storage:
                        storage.headers.clear()
                        storage.auth = lambda request: request  # Do not attach .netrc credentials to signed PUTs.
                        response = storage.put(part['url'], data=_FileSlice(file, length, self),
                                               headers={'Content-Type': 'application/octet-stream',
                                                        'Content-Length': str(length)},
                                               timeout=(5, 85), allow_redirects=False)
                        try:
                            if response.status_code in (429, 503):
                                raise UploadRetryLater(response)
                            if response.status_code != 200:
                                raise ValueError(f'cloud part upload failed ({response.status_code})')
                        finally:
                            response.close()
                    offset += length
            if offset != size:
                raise ValueError('cloud upload parts do not cover the entire file')
        deadline = time.monotonic() + 30
        while not self.stop.is_set() and self.enabled():
            result = accepted(self._post(session, api + '/complete', headers=headers,
                                           timeout=(5, 15), allow_redirects=False))
            if result.get('completed') is True:
                return
            if time.monotonic() >= deadline:
                break
            self.stop.wait(1)
        raise ValueError('cloud upload verification pending')

    def drain_once(self, session):
        if self.backoff.remaining() > 0:
            return
        with self.lock:
            jobs = sorted(self.root.glob('*.pending'))
        for job in jobs:
            if self.backoff.remaining() > 0:
                return
            if self.stop.is_set() or not self.enabled():
                return
            if self.next_try.get(job.name, 0) > time.monotonic():
                continue
            try:
                job = self._upgrade_legacy(job)
                self.send(job, session)
            except UploadDeferred:
                return
            except UploadRetryLater as error:
                delay = self.backoff.throttle(error.retry_after)
                self.next_try[job.name] = time.monotonic() + delay
                logger.warning(f'[云端统计] HTTP {error.status}，上传队列暂停 {delay:.1f} 秒后随机错峰重试，保留全部文件')
                return
            except Exception as error:
                self.next_try[job.name] = time.monotonic() + random.uniform(45, 90)
                logger.warning(f'[云端统计] 上传未完成，保留文件重试: {type(error).__name__}')
                continue
            with self.lock:
                self._remove(job)
                self.next_try.pop(job.name, None)
            self.backoff.success()
            logger.info('[云端统计] 图片组已接收，本地上传暂存已清理')

    def _run(self):
        with requests.Session() as session:
            while not self.stop.is_set():
                self.wake.clear()
                try:
                    with self.lock:
                        for job in self.root.iterdir():
                            if JOB_NAME.fullmatch(job.name) and job.is_dir() and not job.is_symlink():
                                age = time.time() - job.stat().st_mtime
                                if age > (7 * 86400 if job.name.endswith('.pending') else 86400):
                                    self._remove(job)
                                    self.next_try.pop(job.name, None)
                    if UPLOAD_ENABLED and self.enabled():
                        self.drain_once(session)
                except OSError as error:
                    logger.warning(f'[云端统计] 暂存目录暂不可用: {type(error).__name__}')
                self.wake.wait(self.backoff.remaining() or 30)


_queues = {}
_queues_lock = threading.Lock()


def update_policy(config):
    """A reloaded instance can pause existing jobs without starting a worker."""
    name = getattr(config, 'config_name', None)
    with _queues_lock:
        for queue in _queues.values():
            if getattr(queue, 'config_name', None) == name:
                queue.enabled = lambda: getattr(config, 'DropRecord_CloudUpload', False) is True


def queue_for(config):
    device = get_device_id()
    instance = hashlib.md5(f'{device}_{config.config_name}'.encode()).hexdigest()[:16]
    with _queues_lock:
        queue = _queues.get(instance)
        if queue is None:
            queue = CloudUploadQueue(Path('log/cloud_upload') / instance,
                                     enabled=lambda: getattr(config, 'DropRecord_CloudUpload', False) is True)
            queue.config_name = config.config_name
            _queues[instance] = queue
            queue.start()
        else:
            queue.enabled = lambda: getattr(config, 'DropRecord_CloudUpload', False) is True
    return queue, device, instance


def enqueue(config, images, genre, combat_count, collected_at):
    if not UPLOAD_ENABLED:
        return None
    queue, device, instance = queue_for(config)
    project = 'research' if genre in ('research', 'research_refresh') else 'drop'
    scene = 'research_complete' if genre == 'research' else genre
    return queue.enqueue(images, {
        'device_id': device, 'instance_id': instance, 'project': project, 'scene': scene,
        'server': config.SERVER, 'collected_at': collected_at or datetime.now(timezone.utc).isoformat(),
        'combat_count': int(combat_count),
        'submitter': str(getattr(config, 'DropRecord_AzurStatsID', '') or '').encode('utf-8')[:128].decode('utf-8', errors='ignore'),
    })
