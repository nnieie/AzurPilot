"""Bounded disk outbox for A's resumable screenshot protocol.

Screenshots are written before a background thread sends them. Immutable job IDs
and files survive retries/restarts, so A can reuse its original hash record.
"""
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import cv2
import requests

from deploy.api_endpoints import UPLOAD_API_URL
from module.base.device_id import get_device_id
from module.logger import logger

MAX_FILE_BYTES = 10 << 20
MAX_GROUP_BYTES = 500 << 20
MAX_SPOOL_BYTES = 512 << 20
JOB_NAME = re.compile(r'^[0-9a-f-]{36}\.(?:pending|part)$')


class CloudUploadQueue:
    def __init__(self, root, *, spool_bytes=MAX_SPOOL_BYTES, base_url=UPLOAD_API_URL, enabled=lambda: True):
        self.root = Path(root)
        self.limit = spool_bytes
        self.base_url = base_url.rstrip('/')
        self.enabled = enabled
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.next_try = {}

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
                if not sizes:
                    raise ValueError('no screenshots')
                manifest = {**metadata, 'upload_id': upload_id, 'file_sizes': sizes}
                data = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
                if len(data) > 16384:
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
        upload_id = metadata['upload_id']
        if job.name != f'{upload_id}.pending' or not JOB_NAME.fullmatch(job.name):
            raise ValueError('invalid upload manifest')
        for i, expected in enumerate(metadata['file_sizes']):
            if (job / f'{i}.png').stat().st_size != expected:
                raise ValueError('outbox file size changed')
        headers = {'X-AzurPilot-ID': metadata['device_id'], 'User-Agent': 'AzurPilot/2'}

        def accepted(response):
            response.raise_for_status()
            body = response.json()
            if response.status_code not in (200, 202) or body.get('accepted') is not True or body.get('upload_id') != upload_id:
                raise ValueError('unexpected upload acknowledgement')
            return body

        # C only returns A's public address. Resolve it once with the small
        # manifest; send all image bytes and the completion request directly to A.
        base_url = self.base_url
        response = session.post(base_url + '/init', json=metadata, headers=headers,
                                timeout=(5, 15), allow_redirects=False)
        if response.status_code in (307, 308):
            try:
                target = urlsplit(urljoin(base_url + '/init', response.headers.get('Location', '')))
                if (target.scheme not in ('http', 'https') or not target.hostname
                        or target.username is not None or target.password is not None
                        or target.path != '/api/v1/uploads/init' or target.query or target.fragment):
                    raise ValueError('invalid upload redirect')
                target.port  # Validate a numeric port before following the redirect.
                base_url = urlunsplit((target.scheme, target.netloc, '/api/v1/uploads', '', ''))
                if base_url == self.base_url:
                    raise ValueError('upload redirect loop')
            finally:
                response.close()
            response = session.post(base_url + '/init', json=metadata, headers=headers,
                                    timeout=(5, 15), allow_redirects=False)
        accepted(response)
        for i in range(len(metadata['file_sizes'])):
            if self.stop.is_set() or not self.enabled():
                raise ValueError('cloud upload stopped')
            with (job / f'{i}.png').open('rb') as file:
                accepted(session.put(f'{base_url}/{upload_id}/files/{i}', data=file,
                                     headers={**headers, 'Content-Type': 'image/png'},
                                     timeout=(5, 85), allow_redirects=False))
        result = accepted(session.post(f'{base_url}/{upload_id}/complete', headers=headers,
                                       timeout=(5, 15), allow_redirects=False))
        if not re.fullmatch(r'[0-9a-f]{64}', str(result.get('sha256', ''))):
            raise ValueError('missing completed group hash')
        return result

    def drain_once(self, session):
        with self.lock:
            jobs = sorted(self.root.glob('*.pending'))
        for job in jobs:
            if self.stop.is_set() or not self.enabled():
                return
            if self.next_try.get(job.name, 0) > time.monotonic():
                continue
            try:
                self.send(job, session)
            except Exception as error:
                self.next_try[job.name] = time.monotonic() + 60
                logger.warning(f'[云端统计] 上传未完成，保留文件重试: {type(error).__name__}')
                continue
            with self.lock:
                self._remove(job)
                self.next_try.pop(job.name, None)
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
                    if self.enabled():
                        self.drain_once(session)
                except OSError as error:
                    logger.warning(f'[云端统计] 暂存目录暂不可用: {type(error).__name__}')
                self.wake.wait(30)


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
    queue, device, instance = queue_for(config)
    project = 'research' if genre in ('research', 'research_refresh') else 'drop'
    scene = 'research_complete' if genre == 'research' else genre
    return queue.enqueue(images, {
        'device_id': device, 'instance_id': instance, 'project': project, 'scene': scene,
        'server': config.SERVER, 'collected_at': collected_at or datetime.now(timezone.utc).isoformat(),
        'combat_count': int(combat_count),
        'submitter': str(getattr(config, 'DropRecord_AzurStatsID', '') or '').encode('utf-8')[:128].decode('utf-8', errors='ignore'),
    })
