"""Small atomic files with a shared process/thread lock, independent of business SQL."""

import json
import os
import tempfile
from pathlib import Path

from deploy.config_transaction import config_transaction


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_bytes())


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


__all__ = ['atomic_write', 'read_json', 'write_json', 'config_transaction']
