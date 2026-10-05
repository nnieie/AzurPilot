"""只读采样旧部署，在临时安装副本中比较迁移前后的全部载荷。"""
import json
import base64
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from module.statistics import opsi_secure
from module.statistics.opsi_keys import WindowsProvider
from module.statistics.opsi_state import digest


def inventory(vault):
    result = {}
    with vault.reading():
        for path in vault.coordinator.paths():
            if not path.exists():
                continue
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, column, kind in [('cl1_data', 'secure_json', 'cl1'),
                                             ('opsi_items', 'secure_payload', 'loot'),
                                             ('resource_snapshots', 'opsi_payload', 'res'),
                                             ('daily_summary_cl1_events', 'secure_payload', 'daily')]:
                    if table not in tables:
                        continue
                    for index, record in enumerate(conn.execute('SELECT * FROM ' + table +
                                                               (' ORDER BY instance,month' if kind == 'cl1' else ' ORDER BY id'))):
                        row = dict(record)
                        token = row.get(column)
                        context = opsi_secure.row_context(kind, row)
                        payload = vault.open_(kind, token, context) if token else {}
                        if kind == 'cl1':
                            payload = dict(json.loads(row['data_json'] or '{}'), **payload)
                        else:
                            fields = {'loot': opsi_secure.LOOT_SECURE_FIELDS, 'res': opsi_secure.RES_SECURE_FIELDS,
                                      'daily': ('duration_seconds', 'estimated_exp')}[kind]
                            payload = dict({field: row.get(field) for field in fields}, **payload)
                        result[f'{table}/{index}'] = digest(payload)
                if 'daily_summary_periods' in tables:
                    for row in conn.execute('SELECT instance,period_key,report_text FROM daily_summary_periods WHERE report_text IS NOT NULL'):
                        text = row[2]
                        if text.startswith(opsi_secure.BLOB_PREFIX):
                            text = vault.open_('reports', text, vault.report_context(row[0], row[1]))['text']
                        result[f'report/{row[0]}/{row[1]}'] = digest(text)
        for path in vault.coordinator.files():
            raw = path.read_bytes()
            kind = 'archives' if vault.coordinator.is_archive(path) or 'cl1_monthly' in path.name else ('ships' if '.json' in path.name else 'loot')
            if vault.coordinator.is_archive(path) and not raw.startswith(b'{"__opsi_secure_v2__"'):
                payload = {'bytes': base64.b64encode(raw).decode()}
            elif '.json' in path.name or vault.coordinator.is_archive(path):
                wrapper = json.loads(raw)
                if wrapper.get(opsi_secure.WRAPPER_KEY) or wrapper.get(opsi_secure.LEGACY_WRAPPER_KEY):
                    payload = vault.open_(kind, wrapper['payload'], vault.file_context(kind, path))
                else:
                    payload = wrapper
            else:
                payload = vault.open_(kind, raw.decode(), vault.file_context(kind, path))
            result[str(path.relative_to(vault.root))] = digest(payload)
    return result


@unittest.skipUnless(os.environ.get('ALAS_TEST_V1_SOURCE'), '未指定只读 V1 来源')
class RealV1CopyTest(unittest.TestCase):
    def test_complete_real_v1_copy_equal_and_source_keyring_untouched(self):
        source = Path(os.environ['ALAS_TEST_V1_SOURCE']).resolve()
        ring_path = source / 'config' / 'opsi_secure' / 'keyring.json'
        ring = ring_path.read_bytes()
        self.assertEqual(json.loads(ring)['version'], 1)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            (root / 'config' / 'opsi_secure').mkdir(parents=True)
            (root / 'config' / 'opsi_secure' / 'keyring.json').write_bytes(ring)
            for name in ('cl1_data.db', 'azurstats_local.db', 'daily_summary.db'):
                path = source / 'config' / name
                if path.exists():
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original, \
                            closing(sqlite3.connect(root / 'config' / name)) as copy:
                        original.backup(copy)
            from module.statistics.opsi_state import StoreCoordinator
            for path in StoreCoordinator(source).files():
                destination = root / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            provider = WindowsProvider()
            vault = opsi_secure.Vault(root, provider=provider, background_migration=False)
            try:
                before = inventory(vault)
                self.assertTrue(before)
                self.assertTrue(vault.ensure_ready())
                self.assertEqual(before, inventory(vault))
                fresh = opsi_secure.Vault(root, provider=provider, background_migration=False)
                self.assertTrue(fresh.ensure_ready())
                self.assertEqual(before, inventory(fresh))
                self.assertFalse(vault.wipe_path.exists())
            finally:
                provider.delete(vault.slot)
        self.assertEqual(ring_path.read_bytes(), ring)
