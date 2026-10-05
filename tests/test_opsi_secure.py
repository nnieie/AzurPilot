"""统计版本迁移、状态校验、事务恢复与故障隔离测试；全部数据位于临时目录。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import base64
import hashlib
import hmac
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing

@contextmanager
def db(path):
    with closing(sqlite3.connect(path)) as conn, conn:
        yield conn

from module.statistics.opsi_keys import KeyProvider, ProviderUnavailable
from module.statistics.opsi_state import canonical

from module.statistics import opsi_secure

NOW = 1_800_000_000.0


def make_cl1_db(path):
    """构造旧版明文 cl1 库（含大世界战斗数据与委托、科研等非大世界字段）。"""
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE cl1_data (instance TEXT, month TEXT, data_json TEXT, '
        'encrypted_blob BLOB, PRIMARY KEY (instance, month))'
    )
    data = {
        'battle_count': 120,
        'akashi_encounters': 3,
        'akashi_ap': 60,
        'akashi_ap_entries': [{'ts': '2026-09-01T10:00:00', 'amount': 20, 'base': 10, 'count': 2, 'source': 'cl1'}],
        'ap_snapshots': [{'ts': '2026-09-01T10:00:00', 'ap': 131, 'asset': 7500.5, 'source': 'cl1'}],
        'last_ap_notification': {'ts': '2026-09-01T10:00:00', 'ap': 131},
        'yellow_coin_snapshots': [{'ts': '2026-09-01T10:00:00', 'yellow_coin': 500, 'source': 'cl1'}],
        'coins_snapshots': [{'ts': '2026-09-01T10:00:00', 'yellow_coins': 500, 'purple_coins': 20, 'source': 'cl1'}],
        'coins_history_version': 2,
        'coins_cleanup_version': 1,
        'meow_battle_raw_count': 10,
        'meow_battle_count': 5.0,
        'meow_round_times': [{'duration': 60.5, 'hazard_level': 3}],
        'meow_battle_times': [20.5],
        'meow_hazard_stats': {'3': {'battle_raw_count': 4, 'effective_rounds': 2.0, 'round_times': [60.0], 'battle_times': [20.0]}},
        'siren_research_devices': {'cl1': 2, 'meow': {'3': 1}},
        'siren_research_device_entries': [{'ts': '2026-09-01T10:00:00', 'source': 'cl1', 'hazard_level': None}],
        'commission_income_entries': [{'ts': '2026-09-01T09:00:00', 'items': {'Gem': 5}, 'commission_count': 1, 'screenshots': []}],
        'research_drop_entries': [{'ts': '2026-09-01T09:00:00', 'project': 'D-737-MI', 'series': 9, 'items': {'Blueprint': 1}, 'imgid': 'x'}],
        'gem_commission_entries': [],
        'running_gem_commissions': [],
    }
    conn.execute(
        'INSERT INTO cl1_data VALUES (?, ?, ?, NULL)',
        ('inst', '2026-09', json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return data


class MemoryProvider(KeyProvider):
    name = 'isolated-test'

    def __init__(self):
        self.states = {}
        self.offline = False

    def load(self, slot):
        if self.offline:
            raise ProviderUnavailable('offline')
        return json.loads(json.dumps(self.states[slot])) if slot in self.states else None

    def save(self, slot, state):
        if self.offline:
            raise ProviderUnavailable('offline')
        self.states[slot] = json.loads(json.dumps(state))

    def delete(self, slot):
        self.states.pop(slot, None)


def legacy_ring(root, key, wrap=lambda x: x):
    directory = root / 'config' / 'opsi_secure'
    directory.mkdir(parents=True, exist_ok=True)
    ring = {'version': 1, 'wrapped_local': base64.b64encode(wrap(key)).decode(),
            'manifest': {'files': {'obsolete-code-path': 'obsolete-code-hash'}}, 'created': 'old', 'updated': 'old'}
    ring['mac'] = hmac.new(opsi_secure._subkey(key, 'opsi-stats/v1/keyring-mac'), canonical(ring), hashlib.sha256).hexdigest()
    (directory / 'keyring.json').write_bytes(canonical(ring))
    return ring


def legacy_blob(key, kind, data):
    cipher = opsi_secure.AES.new(opsi_secure._subkey(key, 'opsi-stats/v1/' + kind), opsi_secure.AES.MODE_GCM,
                                nonce=os.urandom(12))
    cipher.update(('opsi-stats/v1/' + kind).encode())
    raw, tag = cipher.encrypt_and_digest(canonical(data))
    return opsi_secure.LEGACY_PREFIX + base64.b64encode(cipher.nonce + raw + tag).decode()


class OpsiSecureTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()
        (self.root / 'log' / 'cl1' / 'inst').mkdir(parents=True)
        self.provider = MemoryProvider()
        self.key = os.urandom(32)
        self.dpapi_patch = patch.object(opsi_secure.Vault, '_dpapi', side_effect=lambda x, decrypt=False: x)
        self.dpapi_patch.start()
        self.full = make_cl1_db(self.root / 'config' / 'cl1_data.db')
        make_loot_db(self.root / 'config' / 'azurstats_local.db')
        self.ship = self.root / 'log' / 'cl1' / 'inst' / 'ship_exp_data.json'
        self.ship.write_text(json.dumps({'battle_times': [1, 2, 3]}), encoding='utf-8')
        self.csv = self.root / 'log' / 'azurstat_meowofficer_farming.csv'
        self.csv.write_text('a,b\n1,2\n', encoding='utf-8')
        self.vault = self.new_vault()
        self.previous = opsi_secure._VAULT
        opsi_secure.set_vault(self.vault)

    def tearDown(self):
        opsi_secure.set_vault(self.previous)
        self.dpapi_patch.stop()
        self.directory.cleanup()

    def new_vault(self):
        return opsi_secure.Vault(self.root, provider=self.provider, background_migration=False)

    def read_cl1(self, vault=None):
        vault = vault or self.vault
        with db(vault.cl1_db) as conn:
            public, blob = conn.execute('SELECT data_json,secure_json FROM cl1_data').fetchone()
        secure = vault.open_('cl1', blob, opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'}))
        return {**json.loads(public), **secure}

    def to_v1(self):
        ring = legacy_ring(self.root, self.key)
        with db(self.vault.cl1_db) as conn:
            conn.execute('ALTER TABLE cl1_data ADD COLUMN secure_json TEXT')
            public, data = opsi_secure.partition_cl1(self.full)
            conn.execute('UPDATE cl1_data SET data_json=?,secure_json=?',
                         (json.dumps(public), legacy_blob(self.key, 'cl1', data)))
        with db(self.vault.azurstats_db) as conn:
            for table, column, kind, fields in [('opsi_items', 'secure_payload', 'loot', opsi_secure.LOOT_SECURE_FIELDS),
                                               ('resource_snapshots', 'opsi_payload', 'res', opsi_secure.RES_SECURE_FIELDS)]:
                conn.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' TEXT')
                conn.row_factory = sqlite3.Row
                row = dict(conn.execute('SELECT * FROM ' + table).fetchone())
                data = {k: row[k] for k in fields}
                conn.execute('UPDATE ' + table + ' SET ' + column + '=?,' + ','.join(k + '=NULL' for k in fields),
                             (legacy_blob(self.key, kind, data),))
        self.ship.write_text(json.dumps({opsi_secure.LEGACY_WRAPPER_KEY: True,
                                       'payload': legacy_blob(self.key, 'ships', {'battle_times': [1, 2, 3]})}))
        self.csv.write_text(legacy_blob(self.key, 'loot', {'header': ['a', 'b'], 'rows': [['1', '2']]}))
        return ring

    def test_v1_all_stores_equal_after_v2_and_restart(self):
        self.to_v1()
        self.assertTrue(self.vault.ensure_ready())
        fresh = self.new_vault()
        self.assertTrue(fresh.ensure_ready())
        self.assertEqual(self.read_cl1(fresh), self.full)
        with db(fresh.azurstats_db) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute('SELECT * FROM opsi_items').fetchone())
        data = fresh.open_('loot', row['secure_payload'], opsi_secure.row_context('loot', row))
        self.assertEqual(data['item'], 'PlateGeneralT4')
        self.assertEqual(data['amount'], 3)
        self.assertEqual(fresh.open_('loot', self.csv.read_text(), fresh.file_context('loot', self.csv))['rows'], [['1', '2']])
        wrapper = json.loads(self.ship.read_text())
        self.assertEqual(fresh.open_('ships', wrapper['payload'], fresh.file_context('ships', self.ship)), {'battle_times': [1, 2, 3]})

    def test_failure_before_commit_preserves_v1_bytes(self):
        self.to_v1()
        paths = [self.vault.keyring_path, self.vault.cl1_db, self.vault.azurstats_db, self.ship, self.csv]
        before = {p: p.read_bytes() for p in paths}
        with patch.object(self.vault, '_check_roundtrip', side_effect=OSError('injected')):
            self.assertFalse(self.vault.ensure_ready())
        self.assertEqual(before, {p: p.read_bytes() for p in paths})
        self.assertFalse(self.vault.wipe_path.exists())
        with db(self.vault.cl1_db) as conn:
            blob = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
        self.assertEqual(self.new_vault().open_('cl1', blob), opsi_secure.partition_cl1(self.full)[1])
        self.assertTrue(self.new_vault().ensure_ready())

    def test_failure_after_first_database_commit_restores_v1(self):
        self.to_v1()
        original = self.vault.coordinator.snapshot()
        real = opsi_secure.durable_write
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if Path(path) == self.ship and not failed:
                failed = True
                raise OSError('occupied')
            return real(path, data)

        with patch.object(opsi_secure, 'durable_write', side_effect=fail_once):
            self.assertFalse(self.vault.ensure_ready())
        self.assertEqual(self.vault.coordinator.snapshot(), original)
        self.assertEqual(json.loads(self.vault.keyring_path.read_bytes())['version'], 1)
        self.assertFalse(self.vault.wipe_path.exists())
        self.assertTrue(self.new_vault().ensure_ready())
        self.assertEqual(self.read_cl1(self.new_vault()), self.full)

    def test_upgrade_code_change_is_safe(self):
        self.to_v1()
        self.assertTrue(self.vault.ensure_ready())
        self.assertTrue(opsi_secure.Vault(self.root, protected_files=[self.root / 'nonexistent.py'],
                                        provider=self.provider).ensure_ready())
        self.assertEqual(self.read_cl1(), self.full)
        self.assertFalse(self.vault.wipe_path.exists())

    def assert_no_global_detection(self):
        """库外改动不再触发全局清空：新实例可用、无 wipe 记录、凭据未撤销。"""
        fresh = self.new_vault()
        self.assertTrue(fresh.ensure_ready())
        self.assertFalse(self.vault.wipe_path.exists())
        self.assertIsNotNone(self.provider.load(self.vault.slot))
        return fresh

    def test_raw_deletion_is_not_globally_detected(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute('DELETE FROM opsi_items')
            conn.execute('UPDATE resource_snapshots SET oil=999')
        self.assert_no_global_detection()
        with db(self.vault.azurstats_db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM opsi_items').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT oil FROM resource_snapshots').fetchone()[0], 999)
        with db(self.vault.cl1_db) as conn:
            self.assertIn('research_drop_entries', json.loads(conn.execute('SELECT data_json FROM cl1_data').fetchone()[0]))

    def test_forged_insertion_persists_but_reads_empty(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute("INSERT INTO opsi_items(imgid,instance) VALUES('forged','inst')")
        fresh = self.assert_no_global_detection()
        with db(self.vault.azurstats_db) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM opsi_items WHERE imgid='forged'").fetchone())
        self.assertIsNone(row['secure_payload'])
        self.assertIsNone(fresh.open_or_none('loot', row['secure_payload'], opsi_secure.row_context('loot', row)))

    def test_record_replacement_degrades_that_record(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute("UPDATE opsi_items SET instance='other'")
        fresh = self.assert_no_global_detection()
        with db(self.vault.azurstats_db) as conn:
            row = dict(zip([r[1] for r in conn.execute('PRAGMA table_info(opsi_items)')], conn.execute('SELECT * FROM opsi_items').fetchone()))
        # 行身份参与认证：换实例后该条读不出，其余记录不受影响。
        self.assertIsNone(fresh.open_or_none('loot', row['secure_payload'], opsi_secure.row_context('loot', row)))

    def test_record_exchange_degrades_both_records(self):
        with db(self.vault.azurstats_db) as conn:
            conn.execute("INSERT INTO opsi_items(imgid,instance,item,amount,created_at) VALUES('second','inst','other',9,1789000000)")
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            records = conn.execute('SELECT id,secure_payload FROM opsi_items ORDER BY id').fetchall()
            conn.execute('UPDATE opsi_items SET secure_payload=? WHERE id=?', (records[1][1], records[0][0]))
            conn.execute('UPDATE opsi_items SET secure_payload=? WHERE id=?', (records[0][1], records[1][0]))
        fresh = self.assert_no_global_detection()
        with db(self.vault.azurstats_db) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute('SELECT * FROM opsi_items ORDER BY id'):
                self.assertIsNone(fresh.open_or_none('loot', row['secure_payload'], opsi_secure.row_context('loot', dict(row))))

    def test_added_or_removed_files_do_not_wipe(self):
        self.assertTrue(self.vault.ensure_ready())
        extra = self.root / 'log' / 'azurstat_meowofficer_farming.instance-forged.csv'
        extra.write_bytes(self.csv.read_bytes())
        fresh = self.assert_no_global_detection()
        # 文件的相对路径参与认证：复制的密文在新路径下读不出。
        self.assertIsNone(fresh.open_or_none('loot', extra.read_text(), fresh.file_context('loot', extra)))

    def test_transaction_writes_leave_no_pending_state(self):
        """写入不再走 pending/journal 协议：提交即持久，状态里不残留中间态。"""
        self.assertTrue(self.vault.ensure_ready())
        self.update_count(888)
        self.assertNotIn('pending', self.vault._state)
        self.assertFalse(self.vault.journal_path.exists())
        fresh = self.new_vault()
        self.assertTrue(fresh.ensure_ready())
        self.assertEqual(self.read_cl1(fresh)['battle_count'], 888)

    def test_daily_cleanup_commits_without_touching_other_tables(self):
        path = self.root / 'config' / 'daily_summary.db'
        with db(path) as conn:
            conn.execute('CREATE TABLE daily_summary_cl1_events(id INTEGER PRIMARY KEY, instance TEXT, ts TEXT, duration_seconds REAL, estimated_exp INTEGER)')
            conn.execute("INSERT INTO daily_summary_cl1_events VALUES(1,'inst','2026-09-01',10,20)")
            conn.execute('CREATE TABLE daily_summary_periods(instance TEXT,period_key TEXT,report_text TEXT, PRIMARY KEY(instance,period_key))')
            conn.execute("INSERT INTO daily_summary_periods VALUES('inst','2026-09-01','sentinel report')")
            conn.execute('CREATE TABLE other_business(value TEXT)')
            conn.execute("INSERT INTO other_business VALUES('keep')")
        self.assertTrue(self.vault.ensure_ready())
        with db(path) as conn:
            with self.vault.transaction(conn, path):
                conn.execute('DELETE FROM daily_summary_cl1_events')
                conn.execute('DELETE FROM daily_summary_periods')
        self.assertTrue(self.new_vault().ensure_ready())
        with db(path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM daily_summary_cl1_events').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM daily_summary_periods').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT value FROM other_business').fetchone()[0], 'keep')
        self.assertEqual(self.read_cl1(), self.full)

    def test_old_backup_is_preserved_as_payload_and_regular_expiry_is_authenticated(self):
        path = self.root / 'AzurPilot_Data_Backup' / '2026-09-01' / 'cl1_data.db'
        path.parent.mkdir(parents=True)
        raw = self.vault.cl1_db.read_bytes()
        path.write_bytes(raw)
        self.assertTrue(self.vault.ensure_ready())
        self.assertNotIn(b'akashi_ap_entries', path.read_bytes())
        wrapper = json.loads(path.read_bytes())
        data = self.vault.open_('archives', wrapper['payload'], self.vault.file_context('archives', path))
        self.assertEqual(base64.b64decode(data['bytes']), raw)
        self.vault.remove_files([path])
        self.assertFalse(path.exists())
        self.assertTrue(self.new_vault().ensure_ready())
        path.write_bytes(raw)
        self.assertTrue(self.new_vault().ensure_ready())
        self.assertFalse(self.vault.wipe_path.exists())
        self.assertEqual(path.read_bytes(), raw)

    def test_unknown_future_version_is_not_tampering(self):
        self.assertTrue(self.vault.ensure_ready())
        before = self.vault.cl1_db.read_bytes()
        state = self.provider.load(self.vault.slot)
        self.provider.save(self.vault.slot, dict(state, schema=3))
        self.vault.keyring_path.write_text('{"version":3}')
        self.assertFalse(self.new_vault().ensure_ready())
        self.assertEqual(self.vault.cl1_db.read_bytes(), before)
        self.assertFalse(self.vault.wipe_path.exists())

    def test_archived_json_and_csv_are_retained_without_normal_content(self):
        archive = self.ship.parent / 'cl1_monthly.json.bak'
        archive.write_text(json.dumps({'2026-09': 987}))
        self.assertTrue(self.vault.ensure_ready())
        wrapper = json.loads(archive.read_bytes())
        self.assertTrue(wrapper[opsi_secure.WRAPPER_KEY])
        self.assertNotIn('987', archive.read_text())
        self.assertEqual(self.vault.open_('archives', wrapper['payload'], self.vault.file_context('archives', archive)),
                         {'2026-09': 987})

    def test_bit_flip_degrades_that_record_only(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute("UPDATE opsi_items SET secure_payload=substr(secure_payload,1,length(secure_payload)-1)||'!' ")
        fresh = self.assert_no_global_detection()
        with db(self.vault.azurstats_db) as conn:
            columns = [r[1] for r in conn.execute('PRAGMA table_info(opsi_items)')]
            row = dict(zip(columns, conn.execute('SELECT * FROM opsi_items').fetchone()))
        self.assertIsNone(fresh.open_or_none('loot', row['secure_payload'], opsi_secure.row_context('loot', row)))

    def test_forged_triggers_do_not_wipe_or_block(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute("CREATE TRIGGER forged_delete BEFORE DELETE ON opsi_items BEGIN SELECT RAISE(ABORT,'blocked'); END")
        fresh = self.assert_no_global_detection()
        self.assertTrue(fresh.ensure_ready())

    def test_deleted_database_does_not_wipe(self):
        self.assertTrue(self.vault.ensure_ready())
        self.vault.azurstats_db.unlink()
        fresh = self.assert_no_global_detection()
        self.assertTrue(fresh.ensure_ready())

    def test_descriptor_tamper(self):
        self.assertTrue(self.vault.ensure_ready())
        self.vault.keyring_path.write_text('{}')
        self.assertFalse(self.new_vault().ensure_ready())
        self.assertTrue(self.vault.wipe_path.exists())

    def test_rollback_to_old_bytes_returns_old_values(self):
        """无全量根校验后，整文件回滚不再被发现；回滚后的旧密文照常读出。"""
        self.assertTrue(self.vault.ensure_ready())
        old = self.vault.cl1_db.read_bytes()
        self.update_count(999)
        self.vault.cl1_db.write_bytes(old)
        fresh = self.assert_no_global_detection()
        self.assertEqual(self.read_cl1(fresh)['battle_count'], self.full['battle_count'])

    def update_count(self, count, vault=None):
        vault = vault or self.vault
        context = opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'})
        with db(vault.cl1_db) as conn:
            with vault.transaction(conn, vault.cl1_db):
                blob = vault.seal('cl1', {'battle_count': count}, context)
                conn.execute('UPDATE cl1_data SET secure_json=?', (blob,))

    def test_record_replay_returns_old_values_silently(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.cl1_db) as conn:
            old = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
        self.update_count(123)
        with db(self.vault.cl1_db) as conn:
            conn.execute('UPDATE cl1_data SET secure_json=?', (old,))
        fresh = self.assert_no_global_detection()
        self.assertEqual(self.read_cl1(fresh)['battle_count'], self.full['battle_count'])

    def test_file_rollback_reads_old_content(self):
        self.assertTrue(self.vault.ensure_ready())
        before = self.vault.open_('ships', json.loads(self.ship.read_bytes())['payload'],
                                  self.vault.file_context('ships', self.ship))
        old = self.ship.read_bytes()
        self.vault.write_file('ships', self.ship, {'changed': True}, wrapper=True)
        self.ship.write_bytes(old)
        fresh = self.assert_no_global_detection()
        self.assertEqual(fresh.open_('ships', json.loads(self.ship.read_bytes())['payload'],
                                     fresh.file_context('ships', self.ship)), before)

    def test_binding_dataset_instance_identity_period_installation(self):
        self.assertTrue(self.vault.ensure_ready())
        context = self.vault.context('cl1', 'inst', 'row', '2026-09')
        blob = self.vault.seal('cl1', {'battle_count': 7}, context)
        # 直接校验标签，不让每个参数试验清空后影响其余试验。
        for field, value in [('dataset', 'loot'), ('instance', 'other'), ('identity', 'other'),
                             ('period', '2026-10'), ('installation_id', 'other'), ('algorithm', 'AES-GCM')]:
            aad = dict(context, schema=2, algorithm=opsi_secure.ALGORITHM,
                       installation_id=self.vault._state['installation_id'])
            aad[field] = value
            with self.assertRaises(opsi_secure.IntegrityFailure):
                self.vault._decode('opsi-stats/v2/cl1', blob[len(opsi_secure.BLOB_PREFIX):], aad)

    def test_cross_device_account_and_copied_installation_preserve_original(self):
        self.assertTrue(self.vault.ensure_ready())
        other = opsi_secure.Vault(self.root, provider=MemoryProvider())
        self.assertFalse(other.ensure_ready())
        self.assertFalse(self.vault.wipe_path.exists())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            clone = Path(folder) / 'clone'
            shutil.copytree(self.root, clone)
            copied = opsi_secure.Vault(clone, provider=self.provider)
            self.assertFalse(copied.ensure_ready())
            self.assertFalse(copied.wipe_path.exists())
        self.assertTrue(self.vault.ensure_ready())

    def test_provider_offline_does_not_wipe(self):
        self.assertTrue(self.vault.ensure_ready())
        original = self.vault.cl1_db.read_bytes()
        self.provider.offline = True
        self.assertFalse(self.vault.ensure_ready())
        self.assertEqual(self.vault.cl1_db.read_bytes(), original)
        self.assertFalse(self.vault.wipe_path.exists())
        self.provider.offline = False
        self.assertTrue(self.vault.ensure_ready())

    def test_sqlite_busy_does_not_wipe(self):
        self.assertTrue(self.vault.ensure_ready())
        with db(self.vault.azurstats_db) as conn:
            conn.execute('BEGIN EXCLUSIVE')
            # 解封与写入都不再触碰数据库文件，占用中的库不影响可用性判断，也不触发清空。
            self.assertTrue(self.new_vault().ensure_ready())
            self.assertFalse(self.vault.wipe_path.exists())

    def test_exception_exit_rolls_back_without_pending(self):
        self.assertTrue(self.vault.ensure_ready())
        self.update_count(777)
        fresh = self.new_vault()
        self.assertTrue(fresh.ensure_ready())
        self.assertEqual(self.read_cl1(fresh)['battle_count'], 777)
        self.assertNotIn('pending', self.provider.load(self.vault.slot))
        old = self.read_cl1(fresh)
        with self.assertRaises(RuntimeError), db(fresh.cl1_db) as conn:
            with fresh.transaction(conn, fresh.cl1_db):
                conn.execute('DELETE FROM cl1_data')
                raise RuntimeError('terminated')
        self.assertEqual(self.read_cl1(self.new_vault()), old)

    def test_two_process_style_vaults_and_threads(self):
        self.assertTrue(self.vault.ensure_ready())
        def update(_):
            vault = self.new_vault()
            with db(vault.cl1_db) as conn:
                with vault.transaction(conn, vault.cl1_db):
                    raw = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
                    context = opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'})
                    data = vault.open_('cl1', raw, context)
                    data['battle_count'] += 1
                    conn.execute('UPDATE cl1_data SET secure_json=?', (vault.seal('cl1', data, context),))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, range(12)))
        self.assertEqual(self.read_cl1(self.new_vault())['battle_count'], 132)





def make_loot_db(path):
    """构造旧版明文掉落明细与资源快照库（同一个 azurstats_local.db）。"""
    conn = sqlite3.connect(path)
    conn.execute('''
        CREATE TABLE opsi_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imgid TEXT NOT NULL, server TEXT, zone TEXT, zone_type TEXT,
            zone_id INTEGER, hazard_level INTEGER, item TEXT, amount INTEGER,
            tag TEXT, device_id TEXT, instance TEXT, genre TEXT,
            combat_count INTEGER, created_at INTEGER
        )
    ''')
    conn.execute(
        'INSERT INTO opsi_items (imgid, server, zone, zone_type, zone_id, hazard_level, '
        'item, amount, tag, device_id, instance, genre, combat_count, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('abc123', 'cn', 'NA海域', 'abyssal', 5, 6, 'PlateGeneralT4', 3, 'gold',
         'device-1', 'inst', 'opsi_abyssal', 2, 1789000000),
    )
    conn.execute('''
        CREATE TABLE resource_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, instance TEXT NOT NULL, ts TEXT NOT NULL,
            oil INTEGER, coin INTEGER, gem INTEGER, pt INTEGER, cube INTEGER, core INTEGER,
            medal INTEGER, merit INTEGER, guild_coin INTEGER,
            action_point INTEGER, yellow_coin INTEGER, purple_coin INTEGER
        )
    ''')
    conn.execute(
        'INSERT INTO resource_snapshots (instance, ts, oil, coin, gem, pt, cube, core, medal, '
        'merit, guild_coin, action_point, yellow_coin, purple_coin) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('inst', '2026-09-01T10:00:00', 14000, 180000, 2400, 40000, 380, 1200, 600, 18000, 7500, 131, 500, 20),
    )
    conn.commit()
    conn.close()




if __name__ == '__main__':
    unittest.main()
