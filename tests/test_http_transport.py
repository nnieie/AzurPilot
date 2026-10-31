"""Exercise actual QUIC/TLS connections against isolated local protocol servers."""

import asyncio
import ipaddress
import json
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jh2.config
import jh2.connection
import jh2.events
import niquests
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from qh3.asyncio import QuicConnectionProtocol, serve
from qh3.h3.connection import H3Connection
from qh3.h3.events import DataReceived, HeadersReceived
from qh3.quic.configuration import QuicConfiguration

from module.base import http


@pytest.fixture
def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                           critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return cert_path, key_path


@pytest.mark.parametrize('url, expected', [
    ('https://azurpilot.com', True), ('https://API.AZURPILOT.COM.:443/a', True),
    ('https://stock.nanoda.work/api', True), ('https://nanoda.work', True),
    ('https://azurpilot.com.evil.test', False), ('https://fakeazurpilot.com', False),
    ('https://nanoda.work@evil.test', False), ('ftp://api.azurpilot.com', False),
    ('http://127.0.0.1', False),
])
def test_domain_scope(url, expected):
    assert http.is_service_url(url) is expected


def test_runtime_http_import_does_not_require_deploy():
    # Reproduce a launcher with only an older/partial deploy namespace available.
    code = """import sys, types
legacy = types.ModuleType('deploy')
legacy.__path__ = []
sys.modules['deploy'] = legacy
from module.base import http
assert http.USER_AGENT == 'AzurPilot/2.1'
assert 'deploy.http' not in sys.modules
"""
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


def test_deploy_entrypoints_do_not_load_runtime_http():
    subprocess.run(
        [sys.executable, '-c',
         'import sys; import deploy.git, deploy.Windows.git, deploy.frontend; '
         'assert "deploy.http" not in sys.modules; assert "module.base.http" not in sys.modules; '
         'assert "niquests" not in sys.modules'],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize('download', [False, True])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_first_request_uses_http3_and_correct_headers(certificate, download, asynchronous):
    async def scenario():
        requests = []

        class Protocol(QuicConnectionProtocol):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.http = H3Connection(self._quic)
                self.headers = {}

            def quic_event_received(self, event):
                for message in self.http.handle_event(event):
                    if isinstance(message, HeadersReceived):
                        self.headers[message.stream_id] = dict(message.headers)
                    if isinstance(message, (HeadersReceived, DataReceived)) and message.stream_ended:
                        requests.append(self.headers[message.stream_id])
                        body = json.dumps({k.decode(): v.decode() for k, v in requests[-1].items()}).encode()
                        self.http.send_headers(message.stream_id, [(b':status', b'200'),
                                                                   (b'content-type', b'application/json')])
                        self.http.send_data(message.stream_id, body, end_stream=True)
                        self.transmit()

        config = QuicConfiguration(is_client=False, alpn_protocols=['h3'], max_datagram_frame_size=65536)
        config.load_cert_chain(*map(str, certificate))
        server = await serve('127.0.0.1', 0, configuration=config, create_protocol=Protocol)
        port = server._transport.get_extra_info('sockname')[1]
        url = f'https://127.0.0.1:{port}/test'
        try:
            if asynchronous:
                async with http.AsyncSession(download=download) as session:
                    session.trust_env = False
                    response = await session.post(url, json={'test': 1}, timeout=3,
                                                  verify=str(certificate[0]))
            else:
                def send():
                    with http.Session(download=download) as session:
                        session.trust_env = False
                        return session.post(url, json={'test': 1}, timeout=3, verify=str(certificate[0]))
                response = await asyncio.to_thread(send)
            assert response.http_version == 30
            headers = response.json()
            assert headers['user-agent'] == (http.BROWSER_USER_AGENT if download else http.USER_AGENT)
            assert 'origin' not in headers
            if download:
                assert headers['accept-language'] == http.DOWNLOAD_HEADERS['Accept-Language']
            assert len(requests) == 1
        finally:
            server.close()

    with patch.object(http, 'is_service_url', return_value=True), \
            patch.object(http, '_QUIC_CACHE', niquests.structures.QuicSharedCache(max_size=16)):
        asyncio.run(scenario())


@pytest.mark.parametrize('asynchronous', [False, True])
def test_unavailable_quic_falls_back_to_http2_without_replaying_post(certificate, asynchronous):
    async def scenario():
        requests = []

        async def handle(reader, writer):
            connection = jh2.connection.H2Connection(config=jh2.config.H2Configuration(client_side=False))
            connection.initiate_connection()
            writer.write(connection.data_to_send())
            try:
                while data := await reader.read(65536):
                    for event in connection.receive_data(data):
                        if isinstance(event, jh2.events.RequestReceived):
                            requests.append(dict(event.headers))
                        if isinstance(event, jh2.events.StreamEnded):
                            connection.send_headers(event.stream_id, [(b':status', b'200')])
                            connection.send_data(event.stream_id, b'{"ok":true}', end_stream=True)
                    writer.write(connection.data_to_send())
                    await writer.drain()
            finally:
                writer.close()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*map(str, certificate))
        context.set_alpn_protocols(['h2'])
        server = await asyncio.start_server(handle, '127.0.0.1', 0, ssl=context)
        port = server.sockets[0].getsockname()[1]
        url = f'https://127.0.0.1:{port}/test'
        async with server:
            if asynchronous:
                async with http.AsyncSession() as session:
                    session.trust_env = False
                    response = await session.post(url, json={'test': 1}, timeout=(1, 3),
                                                  verify=str(certificate[0]))
            else:
                def send():
                    with http.Session() as session:
                        session.trust_env = False
                        return session.post(url, json={'test': 1}, timeout=(1, 3), verify=str(certificate[0]))
                response = await asyncio.to_thread(send)
            assert response.http_version == 20
            assert response.json() == {'ok': True}
            assert len(requests) == 1
            assert requests[0][b'user-agent'] == http.USER_AGENT.encode()

    with patch.object(http, 'is_service_url', return_value=True), \
            patch.object(http, '_QUIC_CACHE', niquests.structures.QuicSharedCache(max_size=16)):
        asyncio.run(scenario())


def test_failed_quic_endpoint_is_not_reenabled_for_a_new_session():
    with patch.object(http, '_QUIC_CACHE', niquests.structures.QuicSharedCache(max_size=16)):
        url = 'https://api.azurpilot.com/test'
        http._prepare(url, {}, False)
        # The transport deletes a capability after QUIC negotiation fails.
        del http._QUIC_CACHE[('api.azurpilot.com', 443)]
        with http.Session() as session:
            with patch.object(niquests.Session, 'request', return_value=None):
                session.get(url)
        assert ('api.azurpilot.com', 443) not in http._QUIC_CACHE


@pytest.mark.parametrize('failure', [None, 'status', 'length', 'stream'])
def test_atomic_download_preserves_only_complete_files(tmp_path, failure):
    target = tmp_path / 'cache' / 'agent.tar.gz'
    response = MagicMock()
    response.__enter__.return_value = response
    response.headers = {'Content-Length': '8' if failure == 'length' else '4'}
    response.iter_content.return_value = [b'te', b'st']
    if failure == 'status':
        response.raise_for_status.side_effect = niquests.HTTPError('403')
    elif failure == 'stream':
        def broken_stream(size):
            yield b'te'
            raise niquests.ConnectionError('disconnected')
        response.iter_content.side_effect = broken_stream
    with patch.object(http, 'get', return_value=response) as get:
        if failure:
            with pytest.raises((ValueError, niquests.RequestException)):
                http.download_file('https://atx.azurpilot.com/agent.tar.gz', target)
            assert not target.exists()
        else:
            assert http.download_file('https://atx.azurpilot.com/agent.tar.gz', target) == str(target)
            assert target.read_bytes() == b'test'
            # Cached files avoid a second download.
            http.download_file('https://atx.azurpilot.com/agent.tar.gz', target)
            get.assert_called_once()
        assert get.call_args.kwargs['download'] is True
        assert get.call_args.kwargs['headers'] == {'Accept-Encoding': 'identity'}
        assert not list(target.parent.glob('*.part'))
        response.__exit__.assert_called_once()


@pytest.mark.parametrize('url, service', [
    ('https://templates.azurpilot.com/background.png', True),
    ('https://images.nanoda.work/background.png', True),
    ('https://example.test/background.png', False),
])
def test_background_download_routes_service_domains(url, service):
    from module.api import background_service
    with patch.object(http, 'get') as modern, patch.object(background_service.requests, 'get') as legacy:
        background_service._download_get(url, timeout=5, stream=True)
    assert modern.called is service
    assert legacy.called is not service
    if service:
        assert modern.call_args.kwargs['download'] is True
    assert background_service.USER_AGENT == http.BROWSER_USER_AGENT
