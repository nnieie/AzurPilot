"""宿主统计运行服务的专用传输接口。"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import ssl
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from module.statistics.opsi_keys import KeyProvider, ProviderUnavailable
from module.statistics.opsi_state import canonical, digest


class BrokerProvider(KeyProvider):
    name = 'host-broker'

    def __init__(self, url, ca, certificate, private_key):
        self.url = urlsplit(url)
        if self.url.scheme != 'https' or not self.url.hostname or self.url.path not in ('', '/'):
            raise ProviderUnavailable('宿主服务地址不可用')
        self.tls = ssl.create_default_context(cafile=ca)
        self.tls.load_cert_chain(certificate, private_key)
        self.local = threading.local()

    @classmethod
    def from_environment(cls):
        try:
            from module.statistics.opsi_keys import in_container
            if in_container():
                private_key = Path(os.environ['ALAS_STATISTICS_BROKER_KEY']).resolve()
                if not private_key.is_relative_to(Path('/run/secrets')) or private_key.stat().st_mode & 0o077:
                    raise ProviderUnavailable('客户端凭据必须来自独立的宿主只读凭据挂载')
            return cls(*(os.environ[name] for name in ('ALAS_STATISTICS_BROKER', 'ALAS_STATISTICS_BROKER_CA',
                                                       'ALAS_STATISTICS_BROKER_CERT', 'ALAS_STATISTICS_BROKER_KEY')))
        except (KeyError, OSError, ssl.SSLError) as exc:
            raise ProviderUnavailable('宿主统计服务未就绪') from exc

    def _rpc(self, action, slot, **parameters):
        connection = http.client.HTTPSConnection(self.url.hostname, self.url.port, context=self.tls, timeout=10)
        try:
            request = dict(action=action, slot=slot, lease=getattr(self.local, 'lease', None), **parameters)
            connection.request('POST', '/statistics-runtime', canonical(request), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            value = json.loads(response.read(1024 * 1024 * 1024))
            if response.status != 200:
                if response.status == 422:
                    from module.statistics.opsi_secure import IntegrityFailure
                    raise IntegrityFailure('记录校验失败')
                raise ProviderUnavailable('宿主统计服务暂不可用')
            return value
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ProviderUnavailable('宿主统计服务暂不可用') from exc
        finally:
            connection.close()

    @contextmanager
    def lock(self, slot):
        acquired = self._rpc('acquire', slot)
        token = acquired['lease']
        self.local.lease = token
        stop = threading.Event()
        failed = threading.Event()

        def renew():
            self.local.lease = token
            while not stop.wait(10):
                try:
                    self._rpc('renew', slot)
                except ProviderUnavailable:
                    failed.set()
                    return

        worker = threading.Thread(target=renew, daemon=True)
        worker.start()
        try:
            yield
            if failed.is_set():
                raise ProviderUnavailable('宿主提交租约已中断')
        finally:
            stop.set()
            worker.join(timeout=11)
            try:
                self._rpc('release', slot)
            except ProviderUnavailable:
                pass
            self.local.lease = None

    def load(self, slot):
        state = self._rpc('state', slot)['state']
        self.local.expected = digest(state)
        return state

    def save(self, slot, state):
        self._rpc('commit', slot, state=state, expected=getattr(self.local, 'expected', digest(None)))
        self.local.expected = digest(state)

    def delete(self, slot):
        self._rpc('destroy', slot)
        self.local.expected = digest(None)

    def new_key(self):
        return '@host'

    def key(self, state):
        return None

    def encode(self, slot, state, info, raw, aad):
        return self._rpc('record-write', slot, info=info, data=base64.b64encode(raw).decode(), aad=aad)['record']

    def decode(self, slot, state, info, token, aad):
        return base64.b64decode(self._rpc('record-read', slot, info=info, record=token, aad=aad)['data'], validate=True)

    def prepare_legacy(self, slot, ring):
        self._rpc('legacy-check', slot, ring=ring)

    def legacy_open(self, slot, kind, blob):
        return self._rpc('legacy-read', slot, kind=kind, record=blob)['data']


class HostBroker:
    def __init__(self, provider, grants, lease_seconds=40):
        self.provider = provider
        self.grants = grants
        self.lease_seconds = lease_seconds
        self.mutex = threading.Condition()
        self.leases = {}
        self.legacy = {}

    @staticmethod
    def public(state):
        return dict(state, key='@host') if state and 'key' in state else state

    def dispatch(self, fingerprint, request):
        from module.statistics.opsi_secure import IntegrityFailure, Vault, VaultError
        slot = request['slot']
        if self.grants.get(fingerprint) != slot:
            raise ProviderUnavailable('宿主授权不可用')
        action = request['action']
        with self.mutex:
            now = time.monotonic()
            lease = self.leases.get(slot)
            if action == 'acquire':
                deadline = now + 25
                while lease and lease['until'] > time.monotonic():
                    if time.monotonic() >= deadline:
                        raise ProviderUnavailable('宿主服务正在处理其他请求')
                    self.mutex.wait(timeout=min(1, lease['until'] - time.monotonic()))
                    lease = self.leases.get(slot)
                token = uuid.uuid4().hex
                self.leases[slot] = {'token': token, 'until': time.monotonic() + self.lease_seconds}
                return {'lease': token}
            if not lease or lease['token'] != request.get('lease') or lease['until'] <= now:
                raise ProviderUnavailable('宿主提交租约不可用')
            lease['until'] = now + self.lease_seconds
            if action == 'renew':
                return {}
            if action == 'release':
                self.leases.pop(slot)
                self.legacy.pop(slot, None)
                self.mutex.notify_all()
                return {}
            state = self.provider.load(slot)
            if action == 'state':
                return {'state': self.public(state)}
            if action == 'commit':
                if digest(self.public(state)) != request['expected']:
                    raise ProviderUnavailable('宿主提交状态已变化')
                updated = dict(request['state'])
                if 'key' in updated:
                    updated['key'] = state['key'] if state and 'key' in state else self.provider.new_key()
                self.provider.save(slot, updated)
                return {}
            if action == 'destroy':
                if state and state.get('phase') != 'wiping':
                    raise ProviderUnavailable('宿主状态未撤销')
                self.provider.delete(slot)
                return {}
            if action == 'legacy-check':
                vault = Vault(provider=self.provider, background_migration=False)
                self.legacy[slot] = vault._legacy_key(request['ring'])
                return {}
            if action == 'legacy-read':
                vault = Vault(provider=self.provider, background_migration=False)
                vault._legacy = self.legacy.get(slot)
                if vault._legacy is None:
                    raise ProviderUnavailable('旧环境未就绪')
                return {'data': vault._open_legacy(request['kind'], request['record'])}
            if not state or state.get('phase') == 'wiping':
                raise ProviderUnavailable('宿主状态不可用')
            info, aad = request['info'], request['aad']
            if info == 'opsi-stats/v2/commit':
                if not isinstance(aad, dict) or aad.get('slot') != slot or state['phase'] != 'ready':
                    raise ProviderUnavailable('宿主提交状态不可用')
            elif info == 'opsi-stats/v2/transition':
                if aad != slot or state['phase'] not in ('preparing', 'migration'):
                    raise ProviderUnavailable('宿主迁移状态不可用')
            elif info not in ['opsi-stats/v2/' + kind for kind in ('cl1', 'loot', 'res', 'ships', 'daily', 'reports', 'archives')] or \
                    not isinstance(aad, dict) or aad.get('installation_id') != state['installation_id']:
                raise IntegrityFailure('记录身份不一致')
            if action == 'record-write':
                raw = base64.b64decode(request['data'], validate=True)
                return {'record': self.provider.encode(slot, state, info, raw, aad)}
            if action == 'record-read':
                raw = self.provider.decode(slot, state, info, request['record'], aad)
                return {'data': base64.b64encode(raw).decode()}
            raise VaultError('宿主请求无效')

    def server(self, address, certificate, private_key, ca):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                from module.statistics.opsi_secure import IntegrityFailure
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if self.path != '/statistics-runtime' or not 0 < length <= 1024 * 1024 * 1024:
                        raise ProviderUnavailable('请求无效')
                    fingerprint = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
                    result = broker.dispatch(fingerprint, json.loads(self.rfile.read(length)))
                    status = 200
                except IntegrityFailure:
                    result, status = {}, 422
                except Exception:
                    result, status = {}, 503
                raw = canonical(result)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(address, Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_3
        tls.verify_mode = ssl.CERT_REQUIRED
        tls.load_cert_chain(certificate, private_key)
        tls.load_verify_locations(cafile=ca)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        return server
