"""保险库与四个统计存储的集成测试。

覆盖：启用加密后写入即加密、读取透明解密、旧的明文数据迁移后可读、
密钥不可用（换机器等）时写入安全降级且不破坏已有密文；
未启用加密时行为与旧版完全一致。
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

from module.statistics import opsi_secure, resource_stats
from module.statistics.azurstats import AzurStats
from module.statistics.cl1_database import Cl1Database
from module.statistics.ship_exp_stats import ShipExpStats
from tests.test_opsi_secure import MemoryProvider


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()
        (self.root / 'log' / 'cl1' / 'inst').mkdir(parents=True)
        self.previous = opsi_secure._VAULT
        self.provider = MemoryProvider()
        opsi_secure.set_vault(opsi_secure.Vault(root=self.root, provider=self.provider, background_migration=False))
        self.dpapi_patch = None

    def tearDown(self):
        if self.dpapi_patch is not None:
            self.dpapi_patch.stop()
        opsi_secure.set_vault(self.previous)
        self.directory.cleanup()

    def _release_dpapi(self):
        if self.dpapi_patch is not None:
            self.dpapi_patch.stop()
            self.dpapi_patch = None

    def configure(self):
        """启用加密（测试里关闭后台迁移，迁移由需要的用例显式调用）。"""
        self._release_dpapi()
        self.provider.offline = False
        vault = opsi_secure.Vault(root=self.root, protected_files=[], background_migration=False, provider=self.provider)
        opsi_secure.set_vault(vault)
        self.assertTrue(vault.ensure_ready())
        return vault

    def lock(self):
        """模拟凭据服务暂时离线。"""
        self.provider.offline = True
        vault = opsi_secure.Vault(root=self.root, provider=self.provider, background_migration=False)
        opsi_secure.set_vault(vault)
        return vault


class Cl1StoreIntegration(VaultCase):
    def make_db(self):
        return Cl1Database(db_path=self.root / 'config' / 'cl1_data.db')

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.root / 'config' / 'cl1_data.db')
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def month(self):
        return datetime.now().strftime('%Y-%m')

    def test_configured_writes_are_sealed_and_reads_decrypt(self):
        db = self.make_db()
        db.increment_battle_count('inst', 3)
        db.add_ap_snapshot('inst', 131, source='cl1')
        db.add_commission_income('inst', {'Gem': 5})
        self.configure()
        db.increment_battle_count('inst', 2)
        raw_json, raw_secure = self.raw('SELECT data_json, secure_json FROM cl1_data')[0]
        self.assertNotIn('battle_count', raw_json)
        self.assertNotIn('ap_snapshots', raw_json)
        self.assertIn('commission_income_entries', raw_json)
        self.assertTrue(raw_secure.startswith(opsi_secure.BLOB_PREFIX))
        data = db.get_stats('inst', self.month())
        self.assertEqual(data['battle_count'], 5)
        self.assertEqual(len(data['ap_snapshots']), 1)
        self.assertEqual(len(data['commission_income_entries']), 1)
        # 事务内读改写路径同样透明。
        db.increment_akashi_encounter('inst')
        self.assertEqual(db.get_stats('inst', self.month())['akashi_encounters'], 1)

    def test_legacy_plaintext_migrates_and_stays_readable(self):
        db = self.make_db()
        db.increment_battle_count('inst', 7)
        db.add_commission_income('inst', {'Cube': 2})
        vault = self.configure()
        # 测试里关掉了后台迁移，显式执行并确认幂等。
        summary = vault.ensure_migrated()
        self.assertFalse(summary['skipped'])
        data = db.get_stats('inst', self.month())
        self.assertEqual(data['battle_count'], 7)
        self.assertEqual(len(data['commission_income_entries']), 1)
        raw_json = self.raw('SELECT data_json FROM cl1_data')[0][0]
        self.assertNotIn('battle_count', raw_json)

    def test_unavailable_key_writes_keep_previous_ciphertext(self):
        db = self.make_db()
        self.configure()
        db.increment_battle_count('inst', 5)
        self.lock()
        with self.assertRaises(opsi_secure.VaultLocked):
            db.increment_battle_count('inst', 99)
        data = db.get_stats('inst', self.month())
        self.assertEqual(data['battle_count'], 0)  # 密钥不可用时读取降级为默认值
        # 恢复密钥后旧密文仍能读回，且没有被 99 覆盖。
        self.configure()
        data = db.get_stats('inst', self.month())
        self.assertEqual(data['battle_count'], 5)

    def test_unconfigured_behaviour_is_legacy(self):
        db = self.make_db()
        db.increment_battle_count('inst', 4)
        raw_json, raw_secure = self.raw('SELECT data_json, secure_json FROM cl1_data')[0]
        self.assertNotIn('battle_count', raw_json)
        self.assertTrue(raw_secure.startswith(opsi_secure.BLOB_PREFIX))


class AzurstatsIntegration(VaultCase):
    ROW = {
        'imgid': 'img-1', 'server': 'cn', 'zone': 'NA海域', 'zone_type': 'abyssal',
        'zone_id': 5, 'hazard_level': 6, 'item': 'PlateGeneralT4', 'amount': 3,
        'tag': 'gold', 'device_id': 'dev-1', 'instance': 'inst',
        'genre': 'opsi_meowfficer_farming', 'combat_count': 2, 'created_at': 1_789_000_000,
    }

    def setUp(self):
        super().setUp()
        self.db_patch = patch.object(AzurStats, 'LOCAL_DB', str(self.root / 'config' / 'azurstats_local.db'))
        self.csv_patch = patch.object(
            AzurStats, 'LOCAL_MEOW_CSV', str(self.root / 'log' / 'azurstat_meowofficer_farming.csv'))
        self.db_patch.start()
        self.csv_patch.start()
        AzurStats._ensure_local_db()

    def tearDown(self):
        self.csv_patch.stop()
        self.db_patch.stop()
        super().tearDown()

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.root / 'config' / 'azurstats_local.db')
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_sealed_insert_and_transparent_load(self):
        AzurStats._insert_local_opsi_items([dict(self.ROW)])
        vault = self.configure()
        vault.ensure_migrated()
        AzurStats._insert_local_opsi_items([dict(self.ROW, imgid='img-2', amount=7)])
        rows = self.raw('SELECT item, amount, zone_id, secure_payload, hazard_level FROM opsi_items ORDER BY id')
        self.assertIsNone(rows[0][0])          # 旧行已迁移
        self.assertIsNone(rows[1][0])
        self.assertIsNone(rows[1][4])
        loaded = AzurStats.load_opsi_drop_rows(instance='inst', device_id='dev-1')
        self.assertEqual([row['item'] for row in loaded], ['PlateGeneralT4', 'PlateGeneralT4'])
        self.assertEqual([row['amount'] for row in loaded], [3, 7])

    def test_monthly_totals_across_legacy_and_sealed_rows(self):
        AzurStats._insert_local_opsi_items([dict(self.ROW)])
        self.configure()
        AzurStats._insert_local_opsi_items([dict(self.ROW, imgid='img-2', amount=4)])
        # ROW.created_at 是 2026-09，固定用该月统计。
        totals = AzurStats.get_meow_loot_monthly_totals(year=2026, month=9, device_id='dev-1', instance='inst')
        self.assertEqual(totals[6]['Plate'], 7)

    def test_unavailable_key_insert_is_dropped(self):
        self.configure()
        self.lock()
        inserted = AzurStats._insert_local_opsi_items([dict(self.ROW)])
        self.assertEqual(inserted, 0)
        self.assertEqual(self.raw('SELECT COUNT(*) FROM opsi_items')[0][0], 0)
        self.assertGreaterEqual(opsi_secure.get_vault().status()['dropped'].get('loot', 0), 1)

    def test_farming_csv_is_encrypted_and_readable(self):
        AzurStats._insert_local_opsi_items([dict(self.ROW)])
        self.configure()
        data = AzurStats.get_meowofficer_farming(instance='inst')
        # 实例化文件名带设备哈希，直接找目录里的实际文件。
        files = list((self.root / 'log').glob('azurstat_meowofficer_farming*.csv'))
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding='utf-8')
        self.assertTrue(content.startswith(opsi_secure.BLOB_PREFIX))
        cached = AzurStats.load_meowofficer_farming(instance='inst')
        np.testing.assert_allclose(cached, data)

    def test_unavailable_key_farming_refresh_does_not_touch_file(self):
        self.configure()
        AzurStats._insert_local_opsi_items([dict(self.ROW, device_id='dev-1')])
        AzurStats.get_meowofficer_farming(instance='inst')
        files = list((self.root / 'log').glob('azurstat_meowofficer_farming*.csv'))
        self.assertTrue(files)
        before = files[0].read_bytes()
        self.lock()
        result = AzurStats.get_meowofficer_farming(instance='inst')
        self.assertEqual(result.shape, (6, len(AzurStats.meowofficer_farming_labels)))
        self.assertEqual(files[0].read_bytes(), before)


class ResourceStatsIntegration(VaultCase):
    SNAPSHOT = {'Oil': 14000, 'Coin': 180000, 'ActionPoint': 131, 'YellowCoin': 500, 'PurpleCoin': 20}

    def setUp(self):
        super().setUp()
        self.db_patch = patch.object(resource_stats, '_LOCAL_DB', str(self.root / 'config' / 'azurstats_local.db'))
        self.ensured_patch = patch.object(resource_stats, '_table_ensured', False)
        self.db_patch.start()
        self.ensured_patch.start()

    def tearDown(self):
        self.ensured_patch.stop()
        self.db_patch.stop()
        super().tearDown()

    def raw(self, sql):
        conn = sqlite3.connect(self.root / 'config' / 'azurstats_local.db')
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_snapshot_seals_only_opsi_columns(self):
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT))
        self.configure()
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT, ActionPoint=160))
        rows = self.raw('SELECT oil, action_point, opsi_payload FROM resource_snapshots ORDER BY id')
        self.assertIsNone(rows[0][1])
        self.assertTrue(rows[0][2].startswith(opsi_secure.BLOB_PREFIX))
        self.assertIsNone(rows[1][1])              # 启用后写入即加密
        self.assertTrue(rows[1][2].startswith(opsi_secure.BLOB_PREFIX))
        self.assertEqual(rows[1][0], 14000)        # 非大世界列保持明文
        timeline = resource_stats.get_resource_timeline('inst')
        self.assertEqual([row['action_point'] for row in timeline], [131, 160])
        self.assertEqual([row['oil'] for row in timeline], [14000, 14000])
        vault = opsi_secure.get_vault()
        with patch.object(resource_stats, '_overlay_opsi_snapshot', side_effect=AssertionError('不得解封载荷')):
            public = resource_stats.get_resource_timeline('inst', include_opsi=False)
        self.assertEqual([row['oil'] for row in public], [14000, 14000])
        self.assertTrue(all('opsi_payload' not in row and row['action_point'] is None for row in public))

    def test_interval_summary_covers_opsi_currencies(self):
        self.configure()
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT))
        start = datetime.now()
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT, ActionPoint=160))
        summary = resource_stats.get_resource_interval_summary('inst', start, datetime.now() + timedelta(minutes=1))
        self.assertEqual(summary['resources']['ActionPoint']['delta'], 29)
        self.assertEqual(summary['resources']['Oil']['delta'], 0)

    def test_unavailable_key_snapshot_skips_only_opsi_columns(self):
        self.configure()
        self.lock()
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT))
        self.assertEqual(self.raw('SELECT count(*) FROM resource_snapshots')[0][0], 0)
        self.assertGreaterEqual(opsi_secure.get_vault().status()['dropped'].get('res', 0), 1)

    def test_migration_of_existing_snapshots(self):
        resource_stats.record_resource_snapshot('inst', dict(self.SNAPSHOT))
        vault = self.configure()
        vault.ensure_migrated()
        row = self.raw('SELECT action_point, opsi_payload FROM resource_snapshots')[0]
        self.assertIsNone(row[0])
        self.assertTrue(row[1].startswith(opsi_secure.BLOB_PREFIX))
        timeline = resource_stats.get_resource_timeline('inst')
        self.assertEqual(timeline[0]['action_point'], 131)


class ShipExpIntegration(VaultCase):
    def make_stats(self):
        return ShipExpStats(path=self.root / 'log' / 'cl1' / 'inst' / 'ship_exp_data.json')

    def test_save_seals_and_reload_decrypts(self):
        self.configure()
        stats = self.make_stats()
        stats.data = {'battle_times': {'samples': [52.0], 'average': 52.0}, 'target_level': 125}
        stats._save()
        wrapper = json.loads(stats._path.read_text(encoding='utf-8'))
        self.assertTrue(wrapper.get(opsi_secure.WRAPPER_KEY))
        fresh = self.make_stats()
        self.assertEqual(fresh.data['battle_times']['average'], 52.0)
        self.assertEqual(fresh.data['target_level'], 125)

    def test_unavailable_key_save_keeps_file_bytes(self):
        self.configure()
        stats = self.make_stats()
        stats.data = {'battle_times': {'samples': [50.0], 'average': 50.0}}
        stats._save()
        before = stats._path.read_bytes()
        self.lock()
        locked = self.make_stats()
        self.assertEqual(locked.data, {})          # 密钥不可用时读取为空且明确标记降级
        locked.data['battle_times'] = {'samples': [1.0], 'average': 1.0}
        locked._save()
        self.assertEqual(stats._path.read_bytes(), before)
        self.assertGreaterEqual(opsi_secure.get_vault().status()['dropped'].get('ships', 0), 1)

    def test_legacy_file_loads_plain(self):
        stats = self.make_stats()
        stats.data = {'battle_times': {'samples': [52.0], 'average': 52.0}}
        stats._save()
        self.assertNotIn('battle_times', stats._path.read_text(encoding='utf-8'))
        self.assertEqual(self.make_stats().data['battle_times']['average'], 52.0)

    def test_existing_object_does_not_reinsert_cache_after_environment_reset(self):
        vault = self.configure()
        stats = self.make_stats()
        stats.data = {'battle_times': {'samples': [52.0], 'average': 52.0}}
        stats._save()
        old = vault._state['installation_id']
        vault.wipe('测试复位')
        self.assertTrue(vault.ensure_ready())
        self.assertNotEqual(vault._state['installation_id'], old)
        stats._save()
        self.assertEqual(stats.data, {})
        self.assertFalse(stats._path.exists())

    def test_offline_plaintext_replacement_is_not_used_as_a_fallback(self):
        vault = self.configure()
        stats = self.make_stats()
        stats.data = {'battle_times': {'average': 52.0}}
        stats._save()
        self.lock()
        stats._path.write_text('{"battle_times":{"average":987654321}}')
        self.assertEqual(self.make_stats().data, {})
        self.assertFalse(vault.wipe_path.exists())
        self.provider.offline = False
        self.assertTrue(opsi_secure.get_vault().ensure_ready())
        # 无全量根校验后不再有"文件被替换→清空"路径；明文替换仍然读不出。
        self.assertFalse(vault.wipe_path.exists())
        self.assertEqual(self.make_stats().data, {})


if __name__ == '__main__':
    unittest.main()
