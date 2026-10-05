"""本机凭据与宿主传输的隔离验证。"""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
import multiprocessing
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta, timezone

from module.statistics import opsi_keys, opsi_secure
from module.statistics.opsi_broker import BrokerProvider, HostBroker
from module.statistics.opsi_state import digest
from tests.test_opsi_secure import MemoryProvider, make_cl1_db, legacy_ring, legacy_blob


def process_increment(root, crash=False):
    """crash=True 时在事务提交后立刻退出，验证异常退出后已提交数据仍然完整。"""
    from contextlib import closing
    import sqlite3
    root = Path(root)
    if not root.is_relative_to(Path(tempfile.gettempdir())):
        raise RuntimeError('测试目录必须隔离')
    vault = opsi_secure.Vault(root, provider=opsi_keys.WindowsProvider(), background_migration=False)
    with closing(sqlite3.connect(vault.cl1_db)) as conn:
        with vault.transaction(conn, vault.cl1_db):
            context = opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'})
            blob = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
            data = vault.open_('cl1', blob, context)
            data['battle_count'] += 1
            conn.execute('UPDATE cl1_data SET secure_json=?', (vault.seal('cl1', data, context),))
    if crash:
        os._exit(17)


def process_migrate(root):
    root = Path(root)
    if not root.is_relative_to(Path(tempfile.gettempdir())):
        raise RuntimeError('测试目录必须隔离')
    real = opsi_secure.durable_write
    def terminate(path, data):
        if Path(path).name == 'ship_exp_data.json':
            os._exit(23)
        return real(path, data)
    opsi_secure.durable_write = terminate
    opsi_secure.Vault(root, provider=opsi_keys.WindowsProvider(), background_migration=False).ensure_migrated()


class ProviderTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', '需要 Windows 凭据服务')
    def test_v1_migration_abrupt_exit_recovers_before_reading(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'log' / 'cl1' / 'inst').mkdir(parents=True)
            full = make_cl1_db(root / 'config' / 'cl1_data.db')
            key = os.urandom(32)
            from module.runtime.account_local import dpapi
            legacy_ring(root, key, wrap=dpapi)
            ship = root / 'log' / 'cl1' / 'inst' / 'ship_exp_data.json'
            ship.write_text(json.dumps({opsi_secure.LEGACY_WRAPPER_KEY: True,
                                       'payload': legacy_blob(key, 'ships', {'samples': [9]})}))
            provider = opsi_keys.WindowsProvider()
            vault = opsi_secure.Vault(root, provider=provider, background_migration=False)
            try:
                worker = multiprocessing.get_context('spawn').Process(target=process_migrate, args=(str(root),))
                worker.start()
                worker.join(timeout=30)
                self.assertEqual(worker.exitcode, 23)
                self.assertEqual(provider.load(vault.slot)['phase'], 'migration')
                self.assertTrue(vault.ensure_ready())
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect(vault.cl1_db)) as conn:
                    public, blob = conn.execute('SELECT data_json,secure_json FROM cl1_data').fetchone()
                data = vault.open_('cl1', blob, opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'}))
                self.assertEqual(dict(json.loads(public), **data), full)
                self.assertFalse(vault.wipe_path.exists())
            finally:
                provider.delete(vault.slot)
    @unittest.skipUnless(sys.platform == 'win32', '需要 Windows 凭据服务')
    def test_native_multiprocess_and_abrupt_exit_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            make_cl1_db(root / 'config' / 'cl1_data.db')
            provider = opsi_keys.WindowsProvider()
            vault = opsi_secure.Vault(root, provider=provider, background_migration=False)
            try:
                self.assertTrue(vault.ensure_ready())
                ctx = multiprocessing.get_context('spawn')
                workers = [ctx.Process(target=process_increment, args=(str(root),)) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=30)
                    self.assertEqual(worker.exitcode, 0)
                worker = ctx.Process(target=process_increment, args=(str(root), True))
                worker.start()
                worker.join(timeout=30)
                self.assertEqual(worker.exitcode, 17)
                self.assertTrue(vault.ensure_ready())
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect(vault.cl1_db)) as conn:
                    blob = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
                self.assertEqual(vault.open_('cl1', blob, opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'}))['battle_count'], 125)
            finally:
                provider.delete(vault.slot)
    @unittest.skipUnless(sys.platform == 'win32', '需要 Windows 凭据服务')
    def test_windows_native_roundtrip_and_delete(self):
        provider = opsi_keys.WindowsProvider()
        slot = 'test-' + uuid.uuid4().hex
        state = {'key': provider.new_key(), 'root': 'root', 'generation': 3}
        try:
            self.assertIsNone(provider.load(slot))
            provider.save(slot, state)
            self.assertEqual(provider.load(slot), state)
            raw = provider.key(state)
            self.assertEqual(len(raw), 32)
            self.assertEqual(opsi_keys.WindowsProvider().key(provider.load(slot)), raw)
            state['generation'] = 4
            provider.save(slot, state)
            self.assertEqual(provider.load(slot)['generation'], 4)
        finally:
            provider.delete(slot)
        self.assertIsNone(provider.load(slot))

    def test_macos_dispatch_and_unavailable(self):
        provider = opsi_keys.MacOSProvider()
        with patch.object(provider, '_native', return_value={'key': 'opaque'}) as native:
            self.assertEqual(provider.load('slot'), {'key': 'opaque'})
            provider.save('slot', {'state': 1})
            provider.delete('slot')
            self.assertEqual([call.args[0] for call in native.call_args_list], ['read', 'write', 'read', 'delete'])
        with patch.dict(sys.modules, {'keyring.backends.macOS.api': None}):
            if sys.platform != 'darwin':
                with self.assertRaises(opsi_keys.ProviderUnavailable):
                    provider.load('slot')

    def test_linux_explicit_service_roundtrip_and_locked(self):
        provider = opsi_keys.LinuxProvider()
        backend = Mock()
        backend.get_password.return_value = json.dumps({'key': 'opaque'})
        with patch.object(provider, '_backend', return_value=backend):
            self.assertEqual(provider.load('slot'), {'key': 'opaque'})
            provider.save('slot', {'key': 'opaque'})
            provider.delete('slot')
            backend.set_password.assert_called_once()
            backend.delete_password.assert_called_once()
        with patch.object(opsi_keys.SystemKeyringProvider, '_backend', return_value=backend):
            backend.get_preferred_collection.return_value.is_locked.return_value = True
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                provider.load('slot')

    def test_linux_never_selects_plaintext_or_third_party_backend(self):
        with patch.object(opsi_keys, 'in_container', return_value=False), patch.dict(os.environ, {}, clear=True), \
                patch.object(opsi_keys.sys, 'platform', 'linux'), patch.object(opsi_keys.LinuxTPMProvider, 'available', return_value=False):
            self.assertIsInstance(opsi_keys.get_provider(), opsi_keys.LinuxProvider)
        with patch.object(opsi_keys, 'in_container', return_value=True), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                opsi_keys.get_provider()

    def test_container_does_not_accept_client_credentials_from_data_volume(self):
        with patch.object(opsi_keys, 'in_container', return_value=True), \
                patch.dict(os.environ, {'ALAS_STATISTICS_BROKER_KEY': '/data/client.key'}):
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                BrokerProvider.from_environment()

    def test_linux_tpm_sealed_only_and_no_cleartext_temp_file(self):
        provider = opsi_keys.LinuxTPMProvider()
        key = os.urandom(32)
        commands = []

        def run(*args, data=None):
            nonlocal key
            commands.append(args)
            if args[0] == 'tpm2_create':
                self.assertEqual(len(data), 32)
                key = data
                Path(args[args.index('-u') + 1]).write_bytes(b'public')
                Path(args[args.index('-r') + 1]).write_bytes(b'sealed-private')
            if args[0] == 'tpm2_unseal':
                return key
            return b''
        with patch.object(provider, '_run', side_effect=run):
            sealed = provider.new_key()
            self.assertTrue(sealed.startswith('TPM2:'))
            self.assertNotIn(base64.b64encode(key).decode(), sealed)
            self.assertEqual(provider.key({'key': sealed}), key)
        self.assertIn('fixedtpm|fixedparent|userwithauth|noda', commands[1])

    def test_tpm_failure_does_not_fall_back_to_file(self):
        provider = opsi_keys.LinuxTPMProvider()
        with patch.object(provider, '_run', side_effect=opsi_keys.ProviderUnavailable('offline')):
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                provider.new_key()


def certificates(folder):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'isolated-test-ca')])
    ca = x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()) \
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)) \
        .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True) \
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False) \
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True) \
        .sign(ca_key, hashes.SHA256())
    (folder / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    fingerprint = None
    for name in ('server', 'client', 'unauthorized'):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])) \
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number()) \
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)) \
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
        if name == 'server':
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), False)
        cert = builder.sign(ca_key, hashes.SHA256())
        (folder / (name + '.pem')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder / (name + '.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                               serialization.PrivateFormat.PKCS8,
                                                               serialization.NoEncryption()))
        if name == 'client':
            fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    return fingerprint


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        fingerprint = certificates(self.root)
        self.slot = 'authorized-installation'
        self.host = MemoryProvider()
        self.broker = HostBroker(self.host, {fingerprint: self.slot}, lease_seconds=40)
        self.server = self.broker.server(('localhost', 0), str(self.root / 'server.pem'),
                                         str(self.root / 'server.key'), str(self.root / 'ca.pem'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = self.make_client()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.folder.cleanup()

    def make_client(self, name='client'):
        return BrokerProvider(f'https://localhost:{self.server.server_port}', str(self.root / 'ca.pem'),
                              str(self.root / (name + '.pem')), str(self.root / (name + '.key')))

    def test_mtls_state_records_and_no_root_export(self):
        with self.client.lock(self.slot):
            self.assertIsNone(self.client.load(self.slot))
            state = {'key': '@host', 'installation_id': 'test', 'phase': 'ready', 'generation': 1, 'root': 'a'}
            self.client.save(self.slot, state)
            value = self.client.load(self.slot)
            self.assertEqual(value['key'], '@host')
            self.assertIsNone(self.client.key(value))
            context = {'installation_id': 'test', 'dataset': 'loot', 'schema': 2}
            token = self.client.encode(self.slot, value, 'opsi-stats/v2/loot', b'payload', context)
            self.assertEqual(self.client.decode(self.slot, value, 'opsi-stats/v2/loot', token, context), b'payload')
            with self.assertRaises(opsi_secure.IntegrityFailure):
                self.client.decode(self.slot, value, 'opsi-stats/v2/loot', token, dict(context, dataset='cl1'))
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                self.client._rpc('dump', self.slot)
            state = {'phase': 'wiping', 'installation_id': 'test'}
            self.client.save(self.slot, state)
            self.client.delete(self.slot)
            self.assertIsNone(self.host.load(self.slot))

    def test_full_vault_migration_write_restart_over_mtls(self):
        root = self.root / 'runtime'
        (root / 'config').mkdir(parents=True)
        make_cl1_db(root / 'config' / 'cl1_data.db')
        slot = opsi_keys.installation_slot(root)
        self.broker.grants = {fingerprint: slot for fingerprint in self.broker.grants}
        vault = opsi_secure.Vault(root, provider=self.client, background_migration=False)
        self.assertTrue(vault.ensure_ready())
        import sqlite3
        from contextlib import closing
        context = opsi_secure.row_context('cl1', {'instance': 'inst', 'month': '2026-09'})
        with closing(sqlite3.connect(vault.cl1_db)) as conn:
            with vault.transaction(conn, vault.cl1_db):
                conn.execute('UPDATE cl1_data SET secure_json=?', (vault.seal('cl1', {'battle_count': 333}, context),))
        fresh = opsi_secure.Vault(root, provider=self.make_client(), background_migration=False)
        self.assertTrue(fresh.ensure_ready())
        with closing(sqlite3.connect(vault.cl1_db)) as conn:
            blob = conn.execute('SELECT secure_json FROM cl1_data').fetchone()[0]
        self.assertEqual(fresh.open_('cl1', blob, context), {'battle_count': 333})
        self.assertNotIn('key', json.loads(vault.keyring_path.read_bytes()))

    def test_copied_volume_other_slot_and_unauthorized_certificate(self):
        with self.assertRaises(opsi_keys.ProviderUnavailable), self.client.lock('copied-installation'):
            pass
        other = self.make_client('unauthorized')
        with self.assertRaises(opsi_keys.ProviderUnavailable), other.lock(self.slot):
            pass

    def test_stale_commit_is_rejected(self):
        with self.client.lock(self.slot):
            self.client.load(self.slot)
            state = {'key': '@host', 'phase': 'ready', 'installation_id': 'test', 'root': 'a', 'generation': 1}
            self.client.save(self.slot, state)
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                self.client._rpc('commit', self.slot, state=dict(state, root='old'), expected=digest(None))

    def test_broker_offline_is_normal_fault(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(opsi_keys.ProviderUnavailable), self.client.lock(self.slot):
            pass


if __name__ == '__main__':
    unittest.main()
