"""统计运行环境的本机凭据接口。"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import nullcontext


class ProviderUnavailable(RuntimeError):
    pass


class KeyProvider:
    name = 'abstract'

    def load(self, slot: str) -> dict | None:
        raise NotImplementedError

    def save(self, slot: str, state: dict) -> None:
        raise NotImplementedError

    def delete(self, slot: str) -> None:
        raise NotImplementedError

    def new_key(self) -> str:
        return base64.b64encode(os.urandom(32)).decode('ascii')

    def key(self, state: dict) -> bytes:
        return base64.b64decode(state['key'], validate=True)

    def lock(self, slot):
        return nullcontext()

    def encode(self, slot, state, info, raw, aad):
        from module.statistics.opsi_secure import _encrypt, _subkey
        return _encrypt(_subkey(self.key(state), info), raw, aad)

    def decode(self, slot, state, info, token, aad):
        from module.statistics.opsi_secure import _decrypt, _subkey
        return _decrypt(_subkey(self.key(state), info), token, aad)


class DeviceRootProvider(KeyProvider):
    device_class = ''

    def _device(self):
        from module.statistics import opsi_device_keys
        return getattr(opsi_device_keys, self.device_class)()

    def _use_device(self):
        raise NotImplementedError

    def new_key(self):
        raw = os.urandom(32)
        if not self._use_device():
            return base64.b64encode(raw).decode()
        token = self._device().wrap(raw)
        try:
            restored = self._device().unwrap(token)
            if not hmac.compare_digest(raw, restored):
                raise ProviderUnavailable('本机设备对象回读未通过')
        except ProviderUnavailable:
            self._runtime_key = None
            from module.statistics.opsi_device_keys import unpack_reference
            reference, _ = unpack_reference(token, self._device().prefix)
            try:
                self._device().delete(reference)
            except ProviderUnavailable:
                pass
            raise
        self._runtime_key = (token, restored)
        return token

    def key(self, state):
        token = state['key']
        if not token.startswith(self._device().prefix):
            return super().key(state)
        cached = getattr(self, '_runtime_key', None)
        if cached and cached[0] == token:
            return cached[1]
        raw = self._device().unwrap(token)
        if len(raw) != 32:
            raise ProviderUnavailable('本机设备对象不可用')
        self._runtime_key = (token, raw)
        return raw

    def _check_device(self, state):
        if state and state.get('key', '').startswith(self._device().prefix):
            self._device().check(state['key'])
        return state

    def _reference(self, state):
        if not state:
            return None
        if state.get('device_reference'):
            return state['device_reference']
        token = state.get('key', '')
        if token.startswith(self._device().prefix):
            from module.statistics.opsi_device_keys import unpack_reference
            return unpack_reference(token, self._device().prefix)[0]
        return None

    def _save_device_state(self, slot, state, write):
        if state.get('phase') != 'wiping':
            write(state)
            return
        reference = self._reference(self.load(slot))
        write(dict(state, device_reference=reference) if reference else state)
        self._runtime_key = None
        if reference:
            self._device().delete(reference)

    def _delete_device_state(self, slot, delete):
        reference = self._reference(self.load(slot))
        self._runtime_key = None
        if reference:
            self._device().delete(reference)
        delete()


class WindowsProvider(DeviceRootProvider):
    name = 'windows-current-user'
    device_class = 'WindowsTPM'

    def _use_device(self):
        return self._device().available()

    def _call(self, action, slot, payload=None):
        from ctypes import wintypes as w
        from module.runtime.account_local import dpapi

        class Credential(ctypes.Structure):
            _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                        ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME),
                        ('CredentialBlobSize', w.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                        ('Persist', w.DWORD), ('AttributeCount', w.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]
        try:
            api = ctypes.WinDLL('advapi32', use_last_error=True)
            target = 'AzurPilot/Statistics/' + slot
            api.CredFree.argtypes = [ctypes.c_void_p]
            if action == 'read':
                output = ctypes.POINTER(Credential)()
                api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
                if not api.CredReadW(target, 1, 0, ctypes.byref(output)):
                    if ctypes.get_last_error() == 1168:
                        return None
                    raise ProviderUnavailable('凭据服务不可用')
                try:
                    raw = ctypes.string_at(output.contents.CredentialBlob, output.contents.CredentialBlobSize)
                    return json.loads(dpapi(raw, decrypt=True))
                finally:
                    api.CredFree(output)
            if action == 'delete':
                api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
                if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                    raise ProviderUnavailable('凭据服务不可用')
                return
            raw = dpapi(json.dumps(payload, separators=(',', ':')).encode())
            if len(raw) > 2560:
                raise ProviderUnavailable('凭据状态超过平台容量')
            buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            credential = Credential(Type=1, TargetName=target, CredentialBlobSize=len(raw),
                                    CredentialBlob=buf, Persist=2, UserName='AzurPilot')
            api.CredWriteW.argtypes = [ctypes.POINTER(Credential), w.DWORD]
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise ProviderUnavailable('凭据服务不可用')
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable('本机凭据暂不可用') from exc

    def load(self, slot):
        return self._check_device(self._call('read', slot))

    def save(self, slot, state):
        self._save_device_state(slot, state, lambda value: self._call('write', slot, value))

    def delete(self, slot):
        self._delete_device_state(slot, lambda: self._call('delete', slot))


class SystemKeyringProvider(KeyProvider):
    backend_module = ''
    backend_class = ''

    def _backend(self):
        import importlib
        try:
            backend = getattr(importlib.import_module(self.backend_module), self.backend_class)()
            if backend.priority <= 0:
                raise ProviderUnavailable('本机凭据服务不可用')
            return backend
        except Exception as exc:
            raise ProviderUnavailable('本机凭据服务不可用') from exc

    def load(self, slot):
        try:
            value = self._backend().get_password('AzurPilot.Statistics', slot)
            return json.loads(value) if value else None
        except Exception as exc:
            raise ProviderUnavailable('本机凭据暂不可用') from exc

    def save(self, slot, state):
        try:
            self._backend().set_password('AzurPilot.Statistics', slot, json.dumps(state, separators=(',', ':')))
        except Exception as exc:
            raise ProviderUnavailable('本机凭据暂不可用') from exc

    def delete(self, slot):
        try:
            backend = self._backend()
            if backend.get_password('AzurPilot.Statistics', slot) is not None:
                backend.delete_password('AzurPilot.Statistics', slot)
        except Exception as exc:
            raise ProviderUnavailable('本机凭据暂不可用') from exc


class MacOSProvider(DeviceRootProvider):
    name = 'macos-keychain'
    device_class = 'MacOSEnclave'

    def _use_device(self):
        return os.getenv('ALAS_STATISTICS_SECURE_ENCLAVE') == '1'

    def _native(self, action, slot, state=None):
        try:
            from keyring.backends.macOS import api
            owned = []

            def string(value):
                pointer = api.create_cf(value)
                owned.append(pointer)
                return pointer

            true = ctypes.c_void_p.in_dll(api._found, 'kCFBooleanTrue')
            false = ctypes.c_void_p.in_dll(api._found, 'kCFBooleanFalse')
            release = api._found.CFRelease
            release.argtypes = [ctypes.c_void_p]
            query = dict(kSecClass=api.k_('kSecClassGenericPassword'),
                         kSecAttrService=string('AzurPilot.Statistics'), kSecAttrAccount=string(slot),
                         kSecUseDataProtectionKeychain=true, kSecAttrSynchronizable=false,
                         kSecUseAuthenticationUI=api.k_('kSecUseAuthenticationUIFail'))
            try:
                if action == 'read':
                    query['kSecReturnData'] = true
                    query['kSecMatchLimit'] = api.k_('kSecMatchLimitOne')
                    ref = api.create_query(**query)
                    owned.append(ref)
                    output = ctypes.c_void_p()
                    status = api.SecItemCopyMatching(ref, ctypes.byref(output))
                    if status == -25300:
                        return None
                    if status:
                        raise ProviderUnavailable('本机凭据暂不可用')
                    try:
                        return json.loads(ctypes.string_at(api.CFDataGetBytePtr(output), api.CFDataGetLength(output)))
                    finally:
                        release(output)
                ref = api.create_query(**query)
                owned.append(ref)
                if action == 'delete':
                    status = api.SecItemDelete(ref)
                    if status not in (0, -25300):
                        raise ProviderUnavailable('本机凭据暂不可用')
                    return
                raw = json.dumps(state, separators=(',', ':')).encode()
                create = api._found.CFDataCreate
                create.restype = ctypes.c_void_p
                create.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
                data = create(None, raw, len(raw))
                owned.append(data)
                fields = dict(kSecValueData=data,
                              kSecAttrAccessible=api.k_('kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly'))
                updates = api.create_query(**fields)
                owned.append(updates)
                update = api._sec.SecItemUpdate
                update.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                update.restype = ctypes.c_int32
                status = update(ref, updates)
                if status == -25300:
                    add = api.create_query(**query, **fields)
                    owned.append(add)
                    status = api.SecItemAdd(add, None)
                if status:
                    raise ProviderUnavailable('本机凭据暂不可用')
            finally:
                for pointer in reversed(owned):
                    release(pointer)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable('本机凭据暂不可用') from exc

    def load(self, slot):
        return self._check_device(self._native('read', slot))

    def save(self, slot, state):
        self._save_device_state(slot, state, lambda value: self._native('write', slot, value))

    def delete(self, slot):
        self._delete_device_state(slot, lambda: self._native('delete', slot))


class LinuxProvider(SystemKeyringProvider):
    name = 'linux-secret-service'
    backend_module = 'keyring.backends.SecretService'
    backend_class = 'Keyring'

    def key(self, state):
        if state.get('key', '').startswith('TPM2:'):
            return LinuxTPMProvider().key(state)
        return super().key(state)

    def _backend(self):
        if type(self) is LinuxProvider and os.environ.get('ALAS_STATISTICS_SECRET_SERVICE_VERIFIED') != '1':
            raise ProviderUnavailable('本机凭据服务尚未通过部署检查')
        backend = super()._backend()
        try:
            collection = backend.get_preferred_collection()
            if collection.is_locked():
                raise ProviderUnavailable('本机凭据集合未就绪')
        except Exception as exc:
            raise ProviderUnavailable('本机凭据集合未就绪') from exc
        return backend


class LinuxTPMProvider(LinuxProvider):
    name = 'linux-tpm2-secret-service'

    @staticmethod
    def available():
        return Path('/dev/tpmrm0').exists()

    @staticmethod
    def _run(*args, data=None):
        try:
            command = [args[0], '-T', 'device:/dev/tpmrm0', *args[1:]]
            return subprocess.run(command, input=data, check=True, capture_output=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderUnavailable('本机设备服务暂不可用') from exc

    def load(self, slot):
        state = super().load(slot)
        if state and state.get('key'):
            self._run('tpm2_getcap', 'properties-fixed')
        return state

    def new_key(self):
        with tempfile.TemporaryDirectory(prefix='azurpilot-device-') as folder:
            parent, public, private = [str(Path(folder) / name) for name in ('parent', 'public', 'private')]
            self._run('tpm2_createprimary', '-Q', '-C', 'o', '-G', 'rsa', '-c', parent)
            raw = os.urandom(32)
            self._run('tpm2_create', '-Q', '-C', parent, '-G', 'keyedhash', '-i', '-',
                      '-a', 'fixedtpm|fixedparent|userwithauth|noda', '-u', public, '-r', private, data=raw)
            wrapped = [base64.b64encode(Path(p).read_bytes()).decode() for p in (public, private)]
            token = 'TPM2:' + base64.b64encode(json.dumps(wrapped).encode()).decode()
            if not hmac.compare_digest(raw, self.key({'key': token})):
                self._runtime_key = None
                raise ProviderUnavailable('本机设备对象回读未通过')
            return token

    def key(self, state):
        cached = getattr(self, '_runtime_key', None)
        if cached and cached[0] == state['key']:
            return cached[1]
        with tempfile.TemporaryDirectory(prefix='azurpilot-device-') as folder:
            parent, public, private, loaded = [str(Path(folder) / name)
                                              for name in ('parent', 'public', 'private', 'loaded')]
            wrapped = json.loads(base64.b64decode(state['key'][5:], validate=True))
            for path, blob in zip((public, private), wrapped):
                Path(path).write_bytes(base64.b64decode(blob, validate=True))
            self._run('tpm2_createprimary', '-Q', '-C', 'o', '-G', 'rsa', '-c', parent)
            self._run('tpm2_load', '-Q', '-C', parent, '-u', public, '-r', private, '-c', loaded)
            key = self._run('tpm2_unseal', '-c', loaded)
            if len(key) != 32:
                raise ProviderUnavailable('本机设备状态不可用')
            self._runtime_key = (state['key'], key)
            return key

    def save(self, slot, state):
        if state.get('phase') == 'wiping':
            self._runtime_key = None
        super().save(slot, state)

    def delete(self, slot):
        self._runtime_key = None
        super().delete(slot)


def in_container():
    return Path('/.dockerenv').exists() or Path('/run/.containerenv').exists() or bool(os.getenv('container'))


def get_provider():
    if in_container() or os.getenv('ALAS_STATISTICS_BROKER'):
        from module.statistics.opsi_broker import BrokerProvider
        return BrokerProvider.from_environment()
    if sys.platform == 'win32':
        return WindowsProvider()
    if sys.platform == 'darwin':
        return MacOSProvider()
    if sys.platform == 'linux':
        return linux_provider()
    raise ProviderUnavailable('当前平台没有本机凭据服务')


def linux_provider():
    return LinuxTPMProvider() if LinuxTPMProvider.available() else LinuxProvider()


def installation_slot(root):
    return hashlib.sha256(os.fsencode(str(Path(root).resolve()))).hexdigest()
