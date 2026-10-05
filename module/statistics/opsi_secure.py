"""大世界统计的版本化存储运行服务。"""
from __future__ import annotations
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from Crypto.Cipher import AES, ChaCha20_Poly1305
from module.statistics.opsi_keys import ProviderUnavailable, get_provider, installation_slot
from module.statistics.opsi_state import StoreCoordinator, canonical, durable_write
from module.logger import logger

KINDS = ('cl1', 'ships', 'loot', 'res', 'daily', 'reports', 'archives')

CL1_SECURE_FIELDS = frozenset({
    'battle_count',
    'akashi_encounters',
    'akashi_ap',
    'akashi_ap_entries',
    'ap_snapshots',
    'last_ap_notification',
    'yellow_coin_snapshots',
    'coins_snapshots',
    'coins_history_version',
    'coins_cleanup_version',
    'meow_battle_raw_count',
    'meow_battle_count',
    'meow_round_times',
    'meow_battle_times',
    'meow_hazard_stats',
    'siren_research_devices',
    'siren_research_device_entries',
})

LOOT_SECURE_FIELDS = ('server', 'zone', 'zone_type', 'zone_id', 'item', 'amount', 'tag', 'hazard_level', 'combat_count')

RES_SECURE_FIELDS = ('action_point', 'yellow_coin', 'purple_coin')

LEGACY_PREFIX = 'OPSIV1.'
ALGORITHM = 'XCHACHA20-POLY1305'
BLOB_PREFIX = 'OPSIV2.' + ALGORITHM + '.'
LEGACY_WRAPPER_KEY = '__opsi_secure_v1__'
WRAPPER_KEY = '__opsi_secure_v2__'
MISSING_MARKER = '__opsi_secure_missing__'

MIGRATION_CHUNK = 5000


class VaultError(RuntimeError):
    pass


class VaultLocked(VaultError):
    pass


class IntegrityFailure(VaultError):
    pass


def partition_cl1(data):
    return ({k: v for k, v in data.items() if k not in CL1_SECURE_FIELDS},
            {k: v for k, v in data.items() if k in CL1_SECURE_FIELDS})


def _b64e(data):
    return base64.b64encode(data).decode('ascii')


def _b64d(text):
    return base64.b64decode(text, validate=True)


def _subkey(key, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode()).derive(key)


def _encrypt(key, raw, aad):
    cipher = ChaCha20_Poly1305.new(key=key, nonce=os.urandom(24))
    cipher.update(canonical(aad))
    data, tag = cipher.encrypt_and_digest(raw)
    return _b64e(cipher.nonce + data + tag)


def _decrypt(key, value, aad):
    try:
        raw = _b64d(value)
        if len(raw) < 40:
            raise ValueError()
        cipher = ChaCha20_Poly1305.new(key=key, nonce=raw[:24])
        cipher.update(canonical(aad))
        return cipher.decrypt_and_verify(raw[24:-16], raw[-16:])
    except (ValueError, TypeError) as exc:
        raise IntegrityFailure('记录校验失败') from exc


def row_context(kind, row):
    if kind == 'cl1':
        return {'dataset': kind, 'instance': row['instance'], 'identity': row['month'], 'period': row['month']}
    stamp = row.get('ts') if kind in ('res', 'daily') else row.get('created_at')
    if isinstance(stamp, (int, float)):
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    return {'dataset': kind, 'instance': row.get('instance'), 'identity': str(row['id']),
            'period': str(stamp or '')[:7], 'stamp': stamp,
            'imgid': row.get('imgid'), 'device': row.get('device_id'), 'genre': row.get('genre')}


class Vault:
    def __init__(self, root=None, protected_files=None, clock=time.time, background_migration=True, provider=None):
        self.root = Path(root).resolve() if root else Path(__file__).resolve().parents[2]
        self.directory = self.root / 'config' / 'opsi_secure'
        self.keyring_path = self.directory / 'keyring.json'
        self.wipe_path = self.directory / 'wipe.json'
        self.journal_path = self.directory / 'transition.bin'
        self.cl1_db = self.root / 'config' / 'cl1_data.db'
        self.azurstats_db = self.root / 'config' / 'azurstats_local.db'
        self.coordinator = StoreCoordinator(self.root)
        self.slot = installation_slot(self.root)
        self.provider = provider
        self._state = None
        self._dek = None
        self._legacy = None
        self._active = False
        self._read_warned = set()
        self._dropped = {}
        self._background_migration = background_migration
        self._migration_kicked = False
        self._in_transaction = False
        # 已完成解封的协调锁持有令牌：同一持有期内不重复解封。
        self._fresh = None
        self.coordinator.external_lock = lambda: self._provider().lock(self.slot)

    def _provider(self):
        if self.provider is None:
            self.provider = get_provider()
        return self.provider

    def _keyring(self):
        try:
            value = json.loads(self.keyring_path.read_bytes())
            if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] < 1:
                raise ValueError()
            return value
        except FileNotFoundError:
            return None
        except (ValueError, TypeError) as exc:
            raise IntegrityFailure('统计描述文件无效') from exc

    def keyring_present(self):
        return self.keyring_path.exists()

    def is_configured(self):
        # 无可靠服务时仍禁止写入普通格式。
        return True

    def legacy_plaintext_readable(self):
        return not self._active and (self._legacy is not None or bool(
            self._state and self._state.get('phase') == 'preparing'))

    def _dpapi(self, data, decrypt=False):
        from module.runtime.account_local import dpapi
        return dpapi(data, decrypt=decrypt)

    def _legacy_key(self, ring):
        if hasattr(self._provider(), 'prepare_legacy'):
            self._provider().prepare_legacy(self.slot, ring)
            return b'@host'
        try:
            key = self._dpapi(_b64d(ring['wrapped_local']), decrypt=True)
        except Exception as exc:
            raise VaultLocked('旧统计环境暂不可用') from exc
        payload = {k: v for k, v in ring.items() if k != 'mac'}
        mac_key = _subkey(key, 'opsi-stats/v1/keyring-mac')
        mac = hmac.new(mac_key, canonical(payload), hashlib.sha256).hexdigest()
        if len(key) != 32 or not hmac.compare_digest(mac, str(ring.get('mac', ''))):
            raise VaultLocked('旧统计描述文件校验失败，保留原数据')
        return key

    def _load(self):
        self._active = False
        self._legacy = None
        self._dek = None
        try:
            self._state = self._provider().load(self.slot)
        except ProviderUnavailable:
            ring = self._keyring()
            if ring and ring['version'] == 1:
                self._legacy = self._legacy_key(ring)
                return False
            raise
        self._active = bool(self._state and self._state.get('phase') == 'ready')
        if self._state and self._state.get('schema', 2) > 2:
            self._active = False
            raise VaultLocked('当前软件不支持此统计版本')
        if self._state and self._state.get('phase') != 'wiping' and self._state.get('algorithm') != ALGORITHM:
            self._active = False
            raise VaultLocked('当前软件不支持此统计算法')
        ring = self._keyring()
        if self._state:
            if self._state.get('phase') == 'wiping':
                self._finish_wipe()
                raise VaultLocked('统计环境已重置')
            self._dek = self._provider().key(self._state)
            if self._state.get('phase') == 'migration':
                self._recover_migration()
                ring = self._keyring()
            if self._state.get('phase') == 'preparing':
                if ring and ring['version'] == 1:
                    self._legacy = self._legacy_key(ring)
                return False
            self._active = self._state.get('phase') == 'ready'
            if ring != self._descriptor():
                raise IntegrityFailure('统计描述文件与本机状态不匹配')
            self._active = True
            self._resolve_pending()
            return True
        if ring and ring['version'] >= 2:
            raise VaultLocked('当前设备或账户没有统计环境')
        if ring:
            self._legacy = self._legacy_key(ring)
        return False

    def _descriptor(self):
        return {'version': 2, 'algorithm': ALGORITHM, 'installation_id': self._state['installation_id'],
                'provider': self._provider().name}

    def ensure_ready(self):
        return self._refresh() is not False

    def _refresh(self):
        """解封安全服务并确认可用；同一协调锁持有期内只解封一次。

        写入与读取都不再做全库根校验：全量校验的成本是每次约 2 秒（18 万行），
        无法与"写入无延时"并存。完整性由记录级认证加密保证：文件被拷走或
        被改动一个字节，受影响的记录无法解密，读出降级为空而不是被静默采用。
        清空前会先把原始文件副本与凭据状态移入救援目录。
        """
        try:
            with self.coordinator.lock():
                if self._active and self._fresh == self.coordinator.hold_token:
                    return True
                if self._load():
                    self._fresh = self.coordinator.hold_token
                    return True
                self.ensure_migrated()
                if self._active:
                    self._fresh = self.coordinator.hold_token
                return self._active
        except IntegrityFailure:
            if self._active:
                self._wipe('状态校验失败')
            return False
        except (ProviderUnavailable, VaultLocked, OSError, sqlite3.Error, portalocker_error()):
            return False

    writer_ready = ensure_ready

    def ensure_integrity(self):
        self.ensure_ready()

    def status(self):
        return {'configured': self._active or self.keyring_present(), 'keyringPresent': self.keyring_present(),
                'lastWipe': json.loads(self.wipe_path.read_bytes()) if self.wipe_path.exists() else None,
                'dropped': dict(self._dropped)}

    def record_dropped(self, kind):
        self._dropped[kind] = self._dropped.get(kind, 0) + 1

    def _resolve_pending(self):
        pending = self._state.get('pending')
        if not pending:
            return
        actual = self.coordinator.snapshot()
        if actual == self._state['root'] and actual != pending['root']:
            try:
                token = self.journal_path.read_bytes()
            except FileNotFoundError as exc:
                raise IntegrityFailure('统计提交记录缺失') from exc
            if hashlib.sha256(token).hexdigest() != pending['journal']:
                raise IntegrityFailure('统计提交记录不一致')
            images = json.loads(self._decode('opsi-stats/v2/commit', token.decode(),
                                           {'slot': self.slot, 'generation': pending['generation']}))
            self._restore_images(images, data_only=True)
            actual = self.coordinator.snapshot()
        if actual != pending['root']:
            raise IntegrityFailure('统计提交状态不一致')
        self._state.update(root=pending['root'], generation=pending['generation'])
        self._state.pop('pending', None)
        self._fresh = None
        self._provider().save(self.slot, self._state)
        self.journal_path.unlink(missing_ok=True)

    def _begin_commit(self, new_root, images):
        pending = {'root': new_root, 'generation': self._state['generation'] + 1}
        token = self._encode('opsi-stats/v2/commit', canonical(images),
                             {'slot': self.slot, 'generation': pending['generation']}).encode()
        durable_write(self.journal_path, token)
        pending['journal'] = hashlib.sha256(token).hexdigest()
        self._state['pending'] = pending
        self._provider().save(self.slot, self._state)

    def _end_commit(self):
        pending = self._state.pop('pending')
        self._state.update(root=pending['root'], generation=pending['generation'])
        self._fresh = None
        self._provider().save(self.slot, self._state)
        self.journal_path.unlink(missing_ok=True)

    def context(self, kind, instance, identity, period):
        return {'dataset': kind, 'instance': instance, 'identity': str(identity), 'period': str(period)}

    def report_context(self, instance, period):
        month = re.search(r'\d{4}-\d{2}', period)
        return self.context('reports', instance, period, month[0] if month else period)

    def file_context(self, kind, path, instance=None):
        path = Path(path).resolve()
        relative = str(path.relative_to(self.root)).replace('\\', '/')
        # 缓存与舰船文件覆盖多个月，逻辑周期属于整个历史集合。
        return self.context(kind, instance if instance is not None else relative,
                            relative, 'all-history')

    def check_database(self, path):
        if Path(path).resolve() not in self.coordinator.paths():
            raise VaultLocked('统计数据库不属于当前运行环境')

    def check_file(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise VaultLocked('统计文件不属于当前运行环境')
        valid = self.coordinator.is_archive(path) or (path.parent.parent == self.root / 'log' / 'cl1' and
            path.name in ('ship_exp_data.json', 'ship_exp_data.json.bak', 'cl1_monthly.json', 'cl1_monthly.json.bak')) or (
            path.parent == self.root / 'log' and path.name.startswith('azurstat_meowofficer_farming') and
            (path.name.endswith('.csv') or path.name.endswith('.csv.bak')))
        if not valid:
            raise VaultLocked('统计文件未登记')

    def _seal(self, kind, obj, context):
        if kind not in KINDS or not context or context.get('dataset') != kind:
            raise VaultError('记录身份不完整')
        aad = dict(context, schema=2, algorithm=ALGORITHM, installation_id=self._state['installation_id'])
        return BLOB_PREFIX + self._encode('opsi-stats/v2/' + kind, canonical(obj), aad)

    def _encode(self, info, raw, aad):
        return self._provider().encode(self.slot, self._state, info, raw, aad)

    def _decode(self, info, token, aad):
        return self._provider().decode(self.slot, self._state, info, token, aad)

    def seal(self, kind, obj, context=None):
        with self.coordinator.lock():
            if not self._in_transaction and not self.ensure_ready():
                raise VaultLocked('统计运行环境暂不可用')
            return self._seal(kind, obj, context)

    def _open_legacy(self, kind, blob):
        if hasattr(self._provider(), 'legacy_open'):
            return self._provider().legacy_open(self.slot, kind, blob)
        try:
            raw = _b64d(blob[len(LEGACY_PREFIX):])
            cipher = AES.new(_subkey(self._legacy, 'opsi-stats/v1/' + kind), AES.MODE_GCM, nonce=raw[:12])
            cipher.update(('opsi-stats/v1/' + kind).encode())
            return json.loads(cipher.decrypt_and_verify(raw[12:-16], raw[-16:]))
        except (ValueError, TypeError) as exc:
            raise VaultLocked('旧记录校验失败，保留原数据') from exc

    def open_(self, kind, blob, context=None):
        with self.coordinator.lock():
            try:
                if not self._in_transaction:
                    try:
                        self._load()
                    except IntegrityFailure:
                        raise
                if isinstance(blob, str) and blob.startswith(LEGACY_PREFIX) and not self._active:
                    if self._legacy is None:
                        raise VaultLocked('旧统计环境暂不可用')
                    return self._open_legacy(kind, blob)
                if not self._active:
                    raise VaultLocked('统计运行环境暂不可用')
                if not context or context.get('dataset') != kind or not isinstance(blob, str) or not blob.startswith(BLOB_PREFIX):
                    raise IntegrityFailure('记录身份不一致')
                aad = dict(context, schema=2, algorithm=ALGORITHM, installation_id=self._state['installation_id'])
                return json.loads(self._decode('opsi-stats/v2/' + kind, blob[len(BLOB_PREFIX):], aad))
            except IntegrityFailure:
                if self._active and not self._in_transaction:
                    self._wipe('记录校验失败')
                raise
            except ProviderUnavailable as exc:
                raise VaultLocked('统计运行环境暂不可用') from exc

    def open_or_none(self, kind, blob, context=None):
        try:
            return self.open_(kind, blob, context)
        except (VaultError, ProviderUnavailable, OSError, sqlite3.Error):
            return None

    @contextmanager
    def transaction(self, conn, path):
        self.check_database(path)
        with self.coordinator.lock():
            if self._refresh() is False:
                raise VaultLocked('统计运行环境暂不可用')
            conn.execute('BEGIN IMMEDIATE')
            outer = self._in_transaction
            self._in_transaction = True
            try:
                yield conn
                if conn.execute('PRAGMA foreign_keys').fetchone()[0] and conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('统计事务的延迟约束未满足')
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._in_transaction = outer

    @contextmanager
    def reading(self):
        with self.coordinator.lock():
            outer = self._in_transaction
            try:
                if not outer:
                    if not self._active:
                        if not self._load() and self._legacy is None and self._state is None:
                            self.ensure_ready()
                    if self._active or self._legacy is not None:
                        self._in_transaction = True
                yield
            except IntegrityFailure:
                if self._active and not outer:
                    self._wipe('状态校验失败')
                raise
            finally:
                self._in_transaction = outer

    def write_file(self, kind, path, data, context=None, wrapper=False):
        self.check_file(path)
        with self.coordinator.lock():
            if not self.ensure_ready():
                raise VaultLocked('统计运行环境暂不可用')
            blob = self._seal(kind, data, context or self.file_context(kind, path))
            raw = canonical({WRAPPER_KEY: True, 'payload': blob}) if wrapper else blob.encode()
            durable_write(Path(path).resolve(), raw)

    def remove_files(self, paths):
        with self.coordinator.lock():
            if not self.ensure_ready():
                raise VaultLocked('统计运行环境暂不可用')
            paths = {Path(path).resolve(): None for path in paths}
            if not set(paths).issubset(set(self.coordinator.files())):
                raise VaultError('统计清理路径无效')
            for path in paths:
                path.unlink(missing_ok=True)

    def _kick_migration(self):
        if not self._background_migration or self._migration_kicked:
            return
        self._migration_kicked = True
        threading.Thread(target=self.ensure_ready, name='statistics-transition', daemon=True).start()

    def ensure_migrated(self):
        with self.coordinator.lock():
            if self._load():
                return {'skipped': False}
            return self._migrate()

    def _migrate(self):
        ring = self._keyring()
        before_ring = self.keyring_path.read_bytes() if ring else None
        provider = self._provider()
        if not self._state:
            self._state = {'phase': 'preparing', 'schema': 2, 'algorithm': ALGORITHM, 'installation_id': uuid.uuid4().hex,
                           'key': provider.new_key(), 'root': '', 'generation': 0}
            provider.save(self.slot, self._state)
        self._dek = provider.key(self._state)
        if ring and ring['version'] == 1:
            self._legacy = self._legacy_key(ring)
        connections = {}
        readers = {}
        original, after, overrides = {}, {}, {}
        try:
            for path in self.coordinator.paths():
                if not path.exists():
                    continue
                reader = sqlite3.connect(path, timeout=5)
                readers[path] = reader
                reader.execute('BEGIN IMMEDIATE')
                image = self._standalone_image(reader.serialize())
                original[str(path.relative_to(self.root))] = _b64e(image)
                conn = sqlite3.connect(':memory:')
                connections[path] = conn
                conn.deserialize(image)
                conn.execute('PRAGMA secure_delete=ON')
                self._convert_database(conn)
                conn.commit()
                after[str(path.relative_to(self.root))] = _b64e(conn.serialize())
                overrides[path] = conn
            for path in self.coordinator.files():
                raw = path.read_bytes()
                original[str(path.relative_to(self.root))] = _b64e(raw)
                kind = 'archives' if self.coordinator.is_archive(path) or 'cl1_monthly' in path.name else ('ships' if '.json' in path.name else 'loot')
                context = self.file_context(kind, path)
                if self.coordinator.is_archive(path):
                    data = {'bytes': _b64e(raw)}
                    blob = self._seal(kind, data, context)
                    self._check_roundtrip(kind, blob, data, context)
                    updated = canonical({WRAPPER_KEY: True, 'payload': blob})
                elif '.json' in path.name:
                    data = json.loads(raw)
                    if data.get(LEGACY_WRAPPER_KEY):
                        data = self._open_legacy(kind, data['payload'])
                    blob = self._seal(kind, data, context)
                    self._check_roundtrip(kind, blob, data, context)
                    updated = canonical({WRAPPER_KEY: True, 'payload': blob})
                else:
                    text = raw.decode('utf-8')
                    if text.startswith(LEGACY_PREFIX):
                        data = self._open_legacy('loot', text)
                    else:
                        rows = list(csv.reader(io.StringIO(text)))
                        data = {'header': rows[0], 'rows': rows[1:]}
                    blob = self._seal('loot', data, context)
                    self._check_roundtrip('loot', blob, data, context)
                    updated = blob.encode()
                after[str(path.relative_to(self.root))] = _b64e(updated)
                overrides[path] = updated
            new_root = self.coordinator.snapshot(overrides)
            journal = {'before': original, 'after': after, 'keyring': _b64e(before_ring) if before_ring else None,
                       'descriptor': self._descriptor(), 'root': new_root}
            token = self._encode('opsi-stats/v2/transition', canonical(journal), self.slot).encode()
            durable_write(self.journal_path, token)
            self._state.update(phase='migration', journal=hashlib.sha256(token).hexdigest())
            provider.save(self.slot, self._state)
            for reader in readers.values():
                reader.rollback()
                reader.close()
            readers.clear()
            self._restore_images(after)
            durable_write(self.keyring_path, canonical(self._descriptor()))
            if self.coordinator.snapshot() != new_root:
                raise VaultLocked('迁移回读未通过')
            ready = dict(self._state, phase='ready', root=new_root, generation=1)
            ready.pop('journal', None)
            provider.save(self.slot, ready)
            self._state = ready
            self._active = True
            self._legacy = None
            self.journal_path.unlink(missing_ok=True)
            return {'skipped': False, 'cl1': len(original), 'files': len(after)}
        except BaseException:
            for reader in readers.values():
                reader.rollback()
                reader.close()
            readers.clear()
            for conn in connections.values():
                conn.rollback()
                conn.close()
            connections.clear()
            # 未发布 V2 前的任何失败都不触发清空；可重入恢复原状态。
            if self._state.get('phase') == 'migration':
                self._recover_migration(rollback=True)
            raise
        finally:
            for reader in readers.values():
                reader.close()
            for conn in connections.values():
                conn.close()

    @staticmethod
    def _standalone_image(raw):
        # serialize 已合并当前快照；独立副本不依赖原目录的 WAL。
        if raw[:16] != b'SQLite format 3\x00':
            raise VaultLocked('统计快照不可用')
        return raw[:18] + b'\x01\x01' + raw[20:]

    def _check_roundtrip(self, kind, blob, data, context):
        aad = dict(context, schema=2, algorithm=ALGORITHM, installation_id=self._state['installation_id'])
        decoded = json.loads(self._decode('opsi-stats/v2/' + kind, blob[len(BLOB_PREFIX):], aad))
        if decoded != data:
            raise VaultLocked('迁移回读未通过')

    def _convert_database(self, conn):
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, kind, fields in [('cl1_data', 'secure_json', 'cl1', CL1_SECURE_FIELDS),
                                            ('opsi_items', 'secure_payload', 'loot', LOOT_SECURE_FIELDS),
                                            ('resource_snapshots', 'opsi_payload', 'res', RES_SECURE_FIELDS),
                                            ('daily_summary_cl1_events', 'secure_payload', 'daily',
                                             ('duration_seconds', 'estimated_exp'))]:
            if table not in tables:
                continue
            columns = [r[1] for r in conn.execute('PRAGMA table_info(' + table + ')')]
            if column not in columns:
                conn.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' TEXT')
                columns.append(column)
            for values in conn.execute('SELECT * FROM ' + table).fetchall():
                row = dict(zip(columns, values))
                if kind == 'cl1':
                    full = json.loads(row['data_json']) if row.get('data_json') else None
                    if full is None and row.get('encrypted_blob'):
                        from module.statistics.cl1_legacy import derive_legacy_key, decrypt_legacy_payload
                        from module.base.device_id import get_device_id, get_old_device_id
                        for device in (get_device_id(), get_old_device_id()):
                            if device:
                                try:
                                    full = decrypt_legacy_payload(row['encrypted_blob'], derive_legacy_key(device))
                                except (ValueError, TypeError, UnicodeError):
                                    continue
                                if full:
                                    break
                    if not isinstance(full, dict):
                        raise VaultLocked('旧快照无法读取')
                    public, data = partition_cl1(full)
                else:
                    data = {f: row.get(f) for f in fields}
                if row.get(column):
                    if not row[column].startswith(LEGACY_PREFIX):
                        raise VaultLocked('迁移源格式不支持')
                    data.update(self._open_legacy(kind, row[column]))
                context = row_context(kind, row)
                blob = self._seal(kind, data, context)
                self._check_roundtrip(kind, blob, data, context)
                if kind == 'cl1':
                    conn.execute('UPDATE cl1_data SET data_json=?, secure_json=?, encrypted_blob=NULL WHERE instance=? AND month=?',
                                 (canonical(public).decode(), blob, row['instance'], row['month']))
                else:
                    clear = ','.join(f + ('=0' if kind == 'daily' else '=NULL') for f in fields if f in columns)
                    conn.execute('UPDATE ' + table + ' SET ' + column + '=?,' + clear + ' WHERE id=?', (blob, row['id']))
        if 'daily_summary_periods' in tables:
            for instance, period, text in conn.execute('SELECT instance,period_key,report_text FROM daily_summary_periods WHERE report_text IS NOT NULL').fetchall():
                context = self.report_context(instance, period)
                data = {'text': text}
                blob = self._seal('reports', data, context)
                self._check_roundtrip('reports', blob, data, context)
                conn.execute('UPDATE daily_summary_periods SET report_text=? WHERE instance=? AND period_key=?', (blob, instance, period))

    def _restore_images(self, images, data_only=False):
        for relative, encoded in images.items():
            path = (self.root / relative).resolve()
            allowed_file = (path.parent.parent == self.root / 'log' / 'cl1' and path.name in
                            ('ship_exp_data.json', 'ship_exp_data.json.bak', 'cl1_monthly.json', 'cl1_monthly.json.bak')) or \
                (path.parent == self.root / 'log' and path.name.startswith('azurstat_meowofficer_farming') and
                 (path.name.endswith('.csv') or path.name.endswith('.csv.bak')))
            if not path.is_relative_to(self.root) or (path not in self.coordinator.paths() and not allowed_file and
                                                    not self.coordinator.is_archive(path)):
                raise VaultLocked('迁移记录路径无效')
            if encoded is None:
                path.unlink(missing_ok=True)
                continue
            raw = _b64d(encoded)
            if path in self.coordinator.paths():
                if data_only:
                    with closing(sqlite3.connect(':memory:')) as memory, closing(sqlite3.connect(path, timeout=5)) as target:
                        memory.deserialize(self._standalone_image(raw))
                        self._restore_rows(memory, target)
                else:
                    # 原数据库不写迁移中间值；只发布已经回读通过的完整映像。
                    durable_write(path, self._standalone_image(raw))
                    for suffix in ('-wal', '-shm', '-journal'):
                        Path(str(path) + suffix).unlink(missing_ok=True)
            else:
                durable_write(path, raw)

    @staticmethod
    def _restore_rows(source, target):
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        protected_tables = ('cl1_data', 'opsi_items', 'resource_snapshots', 'daily_summary_cl1_events', 'daily_summary_periods')
        triggers = [row for row in source.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")
                    if row[1] in protected_tables]
        with target:
            target.execute('BEGIN IMMEDIATE')
            for name, table in target.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'").fetchall():
                if table in protected_tables:
                    target.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            for table in protected_tables:
                if table not in tables:
                    continue
                columns = [r[1] for r in source.execute('PRAGMA table_info(' + table + ')')]
                protected = {'cl1_data': ['secure_json', 'encrypted_blob'],
                             'resource_snapshots': ['opsi_payload', 'action_point', 'yellow_coin', 'purple_coin'],
                             'daily_summary_periods': ['report_text']} .get(table)
                if table in ('opsi_items', 'daily_summary_cl1_events'):
                    target.execute('DELETE FROM ' + table)
                else:
                    identity = ['instance', 'month'] if table == 'cl1_data' else \
                        (['instance', 'period_key'] if table == 'daily_summary_periods' else ['id'])
                    present = {tuple(row) for row in source.execute('SELECT ' + ','.join(identity) + ' FROM ' + table)}
                    for key in target.execute('SELECT ' + ','.join(identity) + ' FROM ' + table).fetchall():
                        if tuple(key) not in present:
                            target.execute('DELETE FROM ' + table + ' WHERE ' + ' AND '.join(k + '=?' for k in identity), key)
                for values in source.execute('SELECT * FROM ' + table):
                    row = dict(zip(columns, values))
                    if protected:
                        identity = ['instance', 'month'] if table == 'cl1_data' else \
                            (['instance', 'period_key'] if table == 'daily_summary_periods' else ['id'])
                        condition = ' AND '.join(k + '=?' for k in identity)
                        key = [row[k] for k in identity]
                        if target.execute('SELECT 1 FROM ' + table + ' WHERE ' + condition, key).fetchone():
                            target.execute('UPDATE ' + table + ' SET ' + ','.join(k + '=?' for k in protected)
                                           + ' WHERE ' + condition, [row[k] for k in protected] + key)
                            continue
                    target.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES ('
                                   + ','.join('?' for _ in columns) + ')', values)
            for _, _, sql in triggers:
                target.execute(sql)

    def _recover_migration(self, rollback=True):
        try:
            token = self.journal_path.read_bytes()
            if hashlib.sha256(token).hexdigest() != self._state['journal']:
                raise VaultLocked('迁移记录未通过校验，保留当前数据')
            journal = json.loads(self._decode('opsi-stats/v2/transition', token.decode(), self.slot))
            self._restore_images(journal['before'])
            if journal['keyring']:
                durable_write(self.keyring_path, _b64d(journal['keyring']))
            else:
                self.keyring_path.unlink(missing_ok=True)
            self._state.update(phase='preparing', root='', generation=0)
            self._state.pop('journal', None)
            self._provider().save(self.slot, self._state)
            self._active = False
            self.journal_path.unlink(missing_ok=True)
        except IntegrityFailure as exc:
            raise VaultLocked('迁移记录未通过校验，保留当前数据') from exc

    def _rescue_wipe_copy(self):
        """清空前把受保护文件与凭据状态复制到救援目录，避免误判造成不可恢复的损失。"""
        try:
            folder = self.directory / time.strftime('rescue-%Y%m%d-%H%M%S')
            written = 0
            for path in [*self.coordinator.paths(), *self.coordinator.files()]:
                if not path.exists():
                    continue
                target = folder / f'{written:03d}-{path.name}'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                written += 1
            if self._state:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'provider_state.json').write_bytes(canonical(self._state))
            logger.warning(f'[统计-加密] 清空前已救援备份 {written} 个文件到 {folder}')
        except OSError as exc:
            logger.warning(f'[统计-加密] 清空前救援备份失败: {exc}')

    def _wipe(self, reason):
        with self.coordinator.lock():
            if not self._active:
                return
            # 先撤销凭据，普通 I/O 失败后仍可继续清理，不能再读取旧记录。
            self._rescue_wipe_copy()
            self._fresh = None
            self._state = {'phase': 'wiping', 'installation_id': self._state['installation_id']}
            self._provider().save(self.slot, self._state)
            self._dek = self._legacy = None
            self._active = False
            self._finish_wipe()
            durable_write(self.wipe_path, canonical({'reason': reason, 'at': time.strftime('%Y-%m-%d %H:%M:%S')}))

    def _finish_wipe(self):
        for path in self.coordinator.paths():
            if not path.exists():
                continue
            with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
                conn.execute('PRAGMA secure_delete=ON')
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name, table in conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'").fetchall():
                    if table in ('cl1_data', 'opsi_items', 'resource_snapshots', 'daily_summary_cl1_events', 'daily_summary_periods'):
                        conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
                if 'cl1_data' in tables:
                    for instance, month, text in conn.execute('SELECT instance,month,data_json FROM cl1_data').fetchall():
                        public, _ = partition_cl1(json.loads(text or '{}'))
                        conn.execute('UPDATE cl1_data SET data_json=?, secure_json=NULL, encrypted_blob=NULL WHERE instance=? AND month=?',
                                     (canonical(public).decode(), instance, month))
                if 'opsi_items' in tables:
                    conn.execute('DELETE FROM opsi_items')
                if 'resource_snapshots' in tables:
                    conn.execute('UPDATE resource_snapshots SET opsi_payload=NULL, action_point=NULL, yellow_coin=NULL, purple_coin=NULL')
                if 'daily_summary_cl1_events' in tables:
                    conn.execute('DELETE FROM daily_summary_cl1_events')
                if 'daily_summary_periods' in tables:
                    conn.execute('UPDATE daily_summary_periods SET report_text=NULL')
        for path in self.coordinator.files():
            path.unlink(missing_ok=True)
        self.keyring_path.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)
        self._provider().delete(self.slot)
        self._state = None

    def wipe(self, reason):
        self._wipe(reason)


def portalocker_error():
    import portalocker
    return portalocker.exceptions.LockException


_VAULT = None


def get_vault():
    global _VAULT
    if _VAULT is None:
        _VAULT = Vault()
    return _VAULT


def set_vault(vault):
    global _VAULT
    _VAULT = vault


def is_configured():
    return get_vault().is_configured()


def writer_ready():
    return get_vault().writer_ready()


def seal(kind, obj, context=None):
    return get_vault().seal(kind, obj, context)


def open_(kind, blob, context=None):
    return get_vault().open_(kind, blob, context)


def open_or_none(kind, blob, context=None):
    return get_vault().open_or_none(kind, blob, context)


def ensure_migrated():
    return get_vault().ensure_migrated()


def status():
    return get_vault().status()


def record_dropped(kind):
    get_vault().record_dropped(kind)


def checked_read(function):
    @wraps(function)
    def read(*args, **kwargs):
        vault = get_vault()
        if args and hasattr(args[0], 'db_path'):
            vault.check_database(args[0].db_path)
        elif args and hasattr(args[0], '_path'):
            vault.check_file(args[0]._path)
        elif 'AzurStats' in function.__globals__:
            stats = function.__globals__['AzurStats']
            if function.__name__ == 'load_meowofficer_farming':
                vault.check_file(stats._meowofficer_farming_path(kwargs.get('instance', args[0] if args else None)))
            else:
                vault.check_database(stats.LOCAL_DB)
        elif '_LOCAL_DB' in function.__globals__:
            vault.check_database(function.__globals__['_LOCAL_DB'])
        try:
            with vault.reading():
                return function(*args, **kwargs)
        except (ProviderUnavailable, VaultLocked, OSError, sqlite3.Error):
            # 原调用方的形状及只读降级逻辑保持不变。
            return function(*args, **kwargs)
    return read
