"""统计存储的状态摘要及持久化协调。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import portalocker


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class StoreCoordinator:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'config' / 'opsi_secure'
        self.local = threading.local()
        self.mutex = threading.RLock()
        self.external_lock = None
        # 每次释放最外层协调锁时递增；同一持有期内只解封一次安全服务。
        self.hold_token = 0

    @contextmanager
    def lock(self):
        with self.mutex:
            depth = getattr(self.local, 'depth', 0)
            if depth:
                self.local.depth = depth + 1
                try:
                    yield
                finally:
                    self.local.depth -= 1
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            from contextlib import nullcontext
            with portalocker.Lock(str(self.directory / 'writer.lock'), timeout=30), \
                    (self.external_lock() if self.external_lock else nullcontext()):
                self.local.depth = 1
                try:
                    yield
                finally:
                    self.local.depth = 0
                    self.hold_token += 1

    def files(self):
        cl1 = self.root / 'log' / 'cl1'
        return sorted(set(path for pattern in ('*/ship_exp_data.json', '*/ship_exp_data.json.bak',
                                              '*/cl1_monthly.json', '*/cl1_monthly.json.bak')
                          for path in cl1.glob(pattern))) + sorted(set(path for pattern in
                              ('azurstat_meowofficer_farming*.csv', 'azurstat_meowofficer_farming*.csv.bak')
                              for path in (self.root / 'log').glob(pattern))) + self.archives()

    def is_archive(self, path):
        path = Path(path).resolve()
        relative = path.relative_to(self.root).parts
        return bool(relative and relative[0] == 'AzurPilot_Data_Backup' and (
            path.name in ('cl1_data.db', 'azurstats_local.db', 'daily_summary.db') or
            (path.parent.name == 'opsi_secure' and path.suffix == '.json')))

    def archives(self):
        return sorted(path for path in (self.root / 'AzurPilot_Data_Backup').rglob('*')
                      if path.is_file() and self.is_archive(path))

    def paths(self):
        return [self.root / 'config' / name for name in ('cl1_data.db', 'azurstats_local.db', 'daily_summary.db')]

    def snapshot(self, overrides=None):
        """计算当前全部分量的摘要根；提交阶段必须全量重算，以收编绕开协调的事务外写入。"""
        overrides = overrides or {}
        result = {}
        for path in self.paths():
            if not path.exists() and path not in overrides:
                continue
            if path in overrides:
                result.update(self.database(path, overrides[path]))
            else:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
                    result.update(self.database(path, conn))
        file_paths = set(self.files()) | {p for p in overrides if p not in self.paths()}
        for path in sorted(file_paths):
            raw = overrides[path] if path in overrides else path.read_bytes()
            if raw is not None:
                result[str(path.relative_to(self.root))] = hashlib.sha256(raw).hexdigest()
        return digest(result)

    def database(self, path, conn):
        result = {}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        definitions = {
            'cl1_data': ('instance, month', ['instance', 'month', 'secure_json', 'encrypted_blob', 'data_json']),
            'opsi_items': ('id', None),
            'resource_snapshots': ('id', ['id', 'instance', 'ts', 'opsi_payload',
                                        'action_point', 'yellow_coin', 'purple_coin']),
            'daily_summary_cl1_events': ('id', ['id', 'instance', 'ts', 'duration_seconds',
                                              'estimated_exp', 'secure_payload']),
            'daily_summary_periods': ('instance, period_key', ['instance', 'period_key', 'report_text']),
        }
        triggers = [tuple(row) for row in conn.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
                    if row[1] in definitions]
        if triggers:
            result[str(path.relative_to(self.root)) + '/triggers'] = digest(triggers)
        for table, (order, fields) in definitions.items():
            if table not in tables:
                continue
            columns = [r[1] for r in conn.execute('PRAGMA table_info(' + table + ')')]
            fields = [f for f in (fields or columns) if f in columns]
            hasher = hashlib.sha256()
            count = 0
            for row in conn.execute('SELECT ' + ','.join(fields) + ' FROM ' + table + ' ORDER BY ' + order):
                count += 1
                data = dict(zip(fields, row))
                if table == 'daily_summary_periods' and data.get('report_text') is None:
                    count -= 1
                    continue
                # CL1 的其他业务字段可由现有独立业务事务更新。
                if table == 'cl1_data' and 'data_json' in data:
                    from module.statistics.opsi_secure import partition_cl1
                    parsed = json.loads(data['data_json'] or '{}')
                    data['data_json'] = partition_cl1(parsed)[1]
                for key, value in data.items():
                    if isinstance(value, bytes):
                        data[key] = value.hex()
                hasher.update(canonical(data) + b'\n')
            if count:
                result[str(path.relative_to(self.root)) + '/' + table] = hasher.hexdigest()
        return result


def durable_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + os.urandom(12).hex() + '.stage')
    try:
        with open(temp, 'xb') as stream:
            stream.write(data if isinstance(data, bytes) else data.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temp.unlink(missing_ok=True)
