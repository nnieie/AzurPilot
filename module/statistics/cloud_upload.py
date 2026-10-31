"""Connect local rewards to structured records, template icons and private full screenshots."""

import atexit
import hashlib
import threading
from datetime import datetime, timezone

from module.base.device_id import get_device_id
from module.cloud.service import CloudService
from module.cloud.protocol import CloudError
from module.logger import logger
from module.persistence.database import get_database
from module.statistics.cl1_database import Cl1Database

_queues = {}
_lock = threading.Lock()
SCENE_RECORD_ARGS = {'research': 'ResearchRecord', 'commission': 'CommissionRecord'}


def permitted(config, scene):
    argument = SCENE_RECORD_ARGS.get(scene)
    return (argument is not None
            and getattr(config, 'DropRecord_CloudUpload', False) is True
            and getattr(config, f'DropRecord_{argument}', 'do_not') in ('upload', 'save_and_upload'))


def _bind_policy(queue, config):
    queue.enabled = lambda: any(permitted(config, scene) for scene in SCENE_RECORD_ARGS)
    queue.scene_enabled = lambda scene: permitted(config, scene)


def update_policy(config):
    with _lock:
        for queue in _queues.values():
            if queue.config_name == config.config_name:
                _bind_policy(queue, config)


def resume_pending(config):
    """Resume durable work after a restart without creating a new network queue on construction."""
    if not any(permitted(config, scene) for scene in SCENE_RECORD_ARGS):
        return
    device = get_device_id()
    instance = hashlib.sha256((device + '\0' + config.config_name).encode()).hexdigest()[:16]
    root = get_database().directory / 'cloud' / instance
    if (root / 'catalog' / 'active.json').exists() or any((root / 'jobs').glob('*.json')) \
            or any((root / 'icons').glob('*.json')) or any((root / 'screenshots').glob('*.json')):
        queue_for(config).start()


def queue_for(config):
    database = get_database()
    device = get_device_id()
    instance = hashlib.sha256((device + '\0' + config.config_name).encode()).hexdigest()[:16]
    key = str(database.directory), instance
    with _lock:
        queue = _queues.get(key)
        if queue is None:
            history = Cl1Database(store=database)
            queue = CloudService(database.directory / 'cloud' / instance, device,
                                 apply_mappings=lambda mappings: history.apply_template_mappings(
                                     mappings, instance=config.config_name))
            queue.config_name = config.config_name
            _queues[key] = queue
        _bind_policy(queue, config)
        return queue


def resolver_for(config, scene):
    if not permitted(config, scene):
        return None
    queue = queue_for(config)
    queue.start()
    return RewardResolver(queue, scene)


class RewardResolver:
    def __init__(self, queue, scene):
        self.queue, self.scene = queue, scene

    def __call__(self, image, name):
        return self.queue.identify(image, name, scene=self.scene)

    def capture(self, image, items):
        observations = [
            {'source': item.source_template_id, 'bounds': [int(v) for v in item._button.area]}
            for item in items if item.name.startswith(('U-', 'U?'))
        ]
        try:
            self.queue.capture(image, observations, scene=self.scene)
        except CloudError as error:
            logger.warning(f'[云端统计] 原图队列暂不可用，保留本地统计: {error}')


def enqueue_record(config, scene, items, *, source_items=None, record_id=None,
                   collected_at=None, status=0, **metadata):
    if not permitted(config, scene):
        return None
    stamp = collected_at or datetime.now(timezone.utc)
    if isinstance(stamp, datetime):
        stamp = stamp.astimezone(timezone.utc).isoformat()
    record = {'scene': scene, 'server': config.SERVER, 'status': status,
              'collected_at': stamp, 'items': items, **metadata}
    if source_items is not None:
        record['source_items'] = source_items
    if record_id:
        record['record_id'] = record_id
    return queue_for(config).enqueue(record)


def close_all():
    with _lock:
        for queue in _queues.values():
            queue.close()
        _queues.clear()


atexit.register(close_all)
