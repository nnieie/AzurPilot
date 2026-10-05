"""最终格式、设备对象及普通后端故障的隔离验证。"""
import base64
import ctypes
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from module.statistics import opsi_secure, opsi_keys
from module.statistics.opsi_device_keys import WindowsTPM, MacOSEnclave, pack_reference
from tests.test_opsi_secure import MemoryProvider, make_cl1_db


class FinalFormatTests(unittest.TestCase):
    def test_draft_xchacha_a31_vector_through_runtime_codec(self):
        key = bytes(range(0x80, 0xA0))
        nonce = bytes(range(0x40, 0x58))
        aad = bytes.fromhex('50515253c0c1c2c3c4c5c6c7')
        raw = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, "
               b"sunscreen would be it.")
        expected = bytes.fromhex(
            'bd6d179d3e83d43b9576579493c0e939572a1700252bfaccbed2902c21396cbb'
            '731c7f1b0b4aa6440bf3a82f4eda7e39ae64c6708c54c216cb96b72e1213b452'
            '2f8c9ba40db5d945b11b69b982c1bb9e3f3fac2bc369488f76b2383565d3fff9'
            '21f9664c97637da9768812f615c68b13b52e'
            'c0875924c1c7987947deafd8780acf49')
        with patch.object(opsi_secure, 'canonical', return_value=aad), \
                patch.object(opsi_secure.os, 'urandom', return_value=nonce) as random:
            token = opsi_secure._encrypt(key, raw, {})
            random.assert_called_once_with(24)
            self.assertEqual(base64.b64decode(token), nonce + expected)
            self.assertEqual(opsi_secure._decrypt(key, token, {}), raw)

    def test_random_192_bit_nonce_and_256_bit_key(self):
        key = os.urandom(32)
        tokens = [base64.b64decode(opsi_secure._encrypt(key, b'body', {'id': 1})) for _ in range(32)]
        self.assertEqual(len({token[:24] for token in tokens}), 32)
        self.assertTrue(all(len(token) == 24 + 4 + 16 for token in tokens))
        for length in (16, 24, 31, 33):
            with self.subTest(length=length), self.assertRaises(ValueError):
                opsi_secure._encrypt(bytes(length), b'body', {})

    def test_nonce_payload_tag_and_wrong_key_are_rejected(self):
        key = os.urandom(32)
        token = opsi_secure._encrypt(key, b'body', {'id': 1})
        raw = base64.b64decode(token)
        for index in (0, 23, 24, -1):
            changed = bytearray(raw)
            changed[index] ^= 1
            with self.subTest(index=index), self.assertRaises(opsi_secure.IntegrityFailure):
                opsi_secure._decrypt(key, base64.b64encode(changed).decode(), {'id': 1})
        with self.assertRaises(opsi_secure.IntegrityFailure):
            opsi_secure._decrypt(os.urandom(32), token, {'id': 1})

    def test_format_and_state_identify_only_the_final_algorithm(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            make_cl1_db(root / 'config' / 'cl1_data.db')
            provider = MemoryProvider()
            vault = opsi_secure.Vault(root, provider=provider)
            self.assertTrue(vault.ensure_ready())
            context = vault.context('cl1', 'inst', 'identity', '2026-09')
            blob = vault.seal('cl1', {'battle_count': 8}, context)
            self.assertTrue(blob.startswith('OPSIV2.XCHACHA20-POLY1305.'))
            self.assertEqual(provider.load(vault.slot)['algorithm'], opsi_secure.ALGORITHM)
            self.assertEqual(json.loads(vault.keyring_path.read_bytes())['algorithm'], opsi_secure.ALGORITHM)
            before = vault.cl1_db.read_bytes()
            state = provider.load(vault.slot)
            provider.save(vault.slot, dict(state, algorithm='AES-GCM'))
            self.assertFalse(opsi_secure.Vault(root, provider=provider).ensure_ready())
            self.assertEqual(vault.cl1_db.read_bytes(), before)
            self.assertFalse(vault.wipe_path.exists())


class DeviceRootTests(unittest.TestCase):
    def setUp(self):
        self.reference = uuid.uuid4().hex
        self.key = os.urandom(32)
        self.device = Mock(prefix=WindowsTPM.prefix)
        self.token = pack_reference(WindowsTPM.prefix, self.reference, b'hardware-wrapped')
        self.device.wrap.return_value = self.token
        def wrap(raw):
            self.device.unwrap.return_value = raw
            return self.token
        self.device.wrap.side_effect = wrap
        self.device.unwrap.return_value = self.key
        self.device.available.return_value = True
        self.provider = opsi_keys.WindowsProvider()
        self.states = {}

        def call(action, slot, state=None):
            if action == 'read':
                return dict(self.states[slot]) if slot in self.states else None
            if action == 'write':
                self.states[slot] = dict(state)
            if action == 'delete':
                self.states.pop(slot, None)
        self.native = patch.object(self.provider, '_call', side_effect=call)
        self.native.start()
        self.addCleanup(self.native.stop)
        self.hardware = patch.object(self.provider, '_device', return_value=self.device)
        self.hardware.start()
        self.addCleanup(self.hardware.stop)

    def test_windows_prefers_tpm_and_existing_objects_never_downgrade(self):
        self.assertEqual(self.provider.new_key(), self.token)
        self.device.wrap.assert_called_once()
        self.device.available.return_value = False
        fallback = self.provider.new_key()
        self.assertEqual(len(base64.b64decode(fallback)), 32)
        self.provider._runtime_key = None
        self.device.unwrap.side_effect = opsi_keys.ProviderUnavailable('offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.key({'key': self.token})
        self.device.wrap.assert_called_once()

    def test_transient_creation_failure_has_no_software_fallback(self):
        self.device.available.side_effect = opsi_keys.ProviderUnavailable('tbs offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.new_key()
        self.device.wrap.assert_not_called()
        self.device.available.side_effect = None
        self.device.wrap.side_effect = opsi_keys.ProviderUnavailable('pcp offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.new_key()

    def test_new_device_root_requires_successful_unwrap_before_state_creation(self):
        self.device.wrap.side_effect = None
        self.device.unwrap.side_effect = opsi_keys.ProviderUnavailable('offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.new_key()
        self.device.delete.assert_called_once_with(self.reference)
        self.assertEqual(self.states, {})
        self.assertIsNone(self.provider._runtime_key)

    def test_cached_root_does_not_bypass_device_offline_state_check(self):
        self.provider.new_key()
        self.provider.save('slot', {'key': self.token, 'phase': 'ready'})
        self.device.check.side_effect = opsi_keys.ProviderUnavailable('offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.load('slot')

    def test_wiping_revokes_root_before_hardware_cleanup_and_retry(self):
        self.provider.save('slot', {'key': self.token, 'phase': 'ready', 'root': 'original'})
        self.device.delete.side_effect = opsi_keys.ProviderUnavailable('offline')
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            self.provider.save('slot', {'phase': 'wiping'})
        self.assertNotIn('key', self.states['slot'])
        self.assertNotIn('root', self.states['slot'])
        self.assertEqual(self.states['slot']['device_reference'], self.reference)
        self.device.delete.side_effect = None
        self.provider.delete('slot')
        self.assertNotIn('slot', self.states)
        self.device.delete.assert_called_with(self.reference)

    def test_tpm_offline_preserves_ready_vault_and_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            make_cl1_db(root / 'config' / 'cl1_data.db')
            vault = opsi_secure.Vault(root, provider=self.provider)
            self.assertTrue(vault.ensure_ready())
            before = vault.cl1_db.read_bytes(), vault.keyring_path.read_bytes()
            self.device.check.side_effect = opsi_keys.ProviderUnavailable('offline')
            self.assertFalse(vault.ensure_ready())
            self.assertEqual((vault.cl1_db.read_bytes(), vault.keyring_path.read_bytes()), before)
            self.assertFalse(vault.wipe_path.exists())
            self.assertEqual(self.states[vault.slot]['phase'], 'ready')
            self.device.check.side_effect = None
            self.assertTrue(vault.ensure_ready())

    def test_macos_enclave_opt_in_and_existing_binding_is_enforced(self):
        provider = opsi_keys.MacOSProvider()
        device = Mock(prefix=MacOSEnclave.prefix)
        token = pack_reference(device.prefix, self.reference, b'enclave-wrapped')
        device.wrap.return_value = token
        def wrap(raw):
            device.unwrap.return_value = raw
            return token
        device.wrap.side_effect = wrap
        device.unwrap.return_value = self.key
        with patch.object(provider, '_device', return_value=device), \
                patch.dict(os.environ, {'ALAS_STATISTICS_SECURE_ENCLAVE': '1'}):
            self.assertEqual(provider.new_key(), token)
            device.wrap.side_effect = opsi_keys.ProviderUnavailable('not entitled')
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                provider.new_key()
            provider._runtime_key = None
            device.unwrap.return_value = self.key
            with patch.dict(os.environ, {'ALAS_STATISTICS_SECURE_ENCLAVE': '0'}):
                self.assertEqual(provider.key({'key': token}), self.key)

    def test_host_linux_selection_prefers_tpm(self):
        with patch.object(opsi_keys.LinuxTPMProvider, 'available', return_value=True):
            self.assertIsInstance(opsi_keys.linux_provider(), opsi_keys.LinuxTPMProvider)

    def test_linux_cached_root_still_requires_the_live_device(self):
        provider = opsi_keys.LinuxTPMProvider()
        provider._runtime_key = ('TPM2:opaque', self.key)
        with patch.object(opsi_keys.LinuxProvider, 'load', return_value={'key': 'TPM2:opaque'}), \
                patch.object(provider, '_run', side_effect=opsi_keys.ProviderUnavailable('offline')) as run:
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                provider.load('slot')
            run.assert_called_once_with('tpm2_getcap', 'properties-fixed')

    def test_linux_tss_uses_local_device_without_secret_arguments(self):
        with patch.object(opsi_keys.subprocess, 'run', return_value=Mock(stdout=b'')) as run:
            opsi_keys.LinuxTPMProvider._run('tpm2_create', '-i', '-', data=self.key)
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ['tpm2_create', '-T', 'device:/dev/tpmrm0'])
            self.assertEqual(run.call_args.kwargs['input'], self.key)
            self.assertNotIn(self.key, command)

    def test_windows_tbs_not_found_and_transient_error_are_distinct(self):
        library = Mock()
        with patch.object(ctypes, 'WinDLL', return_value=library, create=True):
            library.Tbsi_GetDeviceInfo.return_value = 0x8028400F
            self.assertFalse(WindowsTPM.available())
            library.Tbsi_GetDeviceInfo.return_value = 0x80284008
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                WindowsTPM.available()

    @unittest.skipUnless(sys.platform == 'win32', '需要 Windows CNG')
    def test_native_cng_non_exportable_restart_and_wrong_object(self):
        if not WindowsTPM.available():
            self.skipTest('本机没有 TPM2')
        hardware = WindowsTPM()
        token = hardware.wrap(self.key)
        from module.statistics.opsi_device_keys import unpack_reference
        reference, data = unpack_reference(token, hardware.prefix)
        try:
            self.assertNotIn(base64.b64encode(self.key).decode(), token)
            self.assertEqual(WindowsTPM().unwrap(token), self.key)
            with hardware._provider() as (api, provider):
                handle = ctypes.c_size_t()
                hardware._check(api.NCryptOpenKey(provider, ctypes.byref(handle), hardware._name(reference), 0, hardware.silent))
                try:
                    policy, size = ctypes.c_uint32(), ctypes.c_uint32()
                    hardware._check(api.NCryptGetProperty(handle, 'Export Policy', ctypes.byref(policy), 4, ctypes.byref(size), 0))
                    self.assertEqual(policy.value, 0)
                finally:
                    api.NCryptFreeObject(handle)
            wrong = pack_reference(hardware.prefix, uuid.uuid4().hex, data)
            with self.assertRaises(opsi_keys.ProviderUnavailable):
                WindowsTPM().unwrap(wrong)
        finally:
            hardware.delete(reference)
        with self.assertRaises(opsi_keys.ProviderUnavailable):
            WindowsTPM().unwrap(token)
        hardware.delete(reference)
