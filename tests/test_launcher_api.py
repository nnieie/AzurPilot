"""启动器与浏览器的真实 WS 回归，全部配置使用临时目录。"""
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from module.api.app import create_app
from module.api.protocol import ApiError
from module.api.launcher_protocol import LauncherCommand, LauncherEvent
from tests.test_api import fixture

HEADERS = {'Authorization': 'Bearer test-secret', 'host': '127.0.0.1:25548'}
HELLO = {'protocolVersion': 2, 'launcherVersion': '3.0.1', 'platform': 'win32',
         'capabilities': ['autostart', 'notifications', 'login'],
         'autostart': {'supported': True, 'enabled': False}}


def call(ws, method, params=None, request_id=None):
    request_id = request_id or method
    ws.send_json({'type': 'request', 'id': request_id, 'method': method, 'params': params or {}})
    return ws.receive_json()


class LauncherApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict('os.environ', {'AP_WEBUI_TRUST_SECRET': 'test-secret'}):
            self.app = create_app(root=fixture(self.temp.name), password='private-password',
                                  manage_runtime=False, mount_mcp=False)
        self.service = self.app.state.gateway.router.launcher
        self.client = TestClient(self.app, base_url='http://127.0.0.1:25548', client=('127.0.0.1', 50000))

    def connect(self):
        return self.client.websocket_connect('/api/v2/launcher/ws', headers=HEADERS)

    def hello(self, ws):
        reply = call(ws, 'session.hello', HELLO)
        self.assertTrue(reply['ok'], reply)

    def test_handshake_rejects_bad_secret_origins_forwarding_and_missing_secret(self):
        for extra in ({'Authorization': 'Bearer wrong'}, {'Origin': 'https://evil.example'},
                      {'x-forwarded-for': '203.0.113.1'}, {'Authorization': 'test-secret'},
                      {'host': 'remote.example'}):
            with self.subTest(extra=extra), self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('/api/v2/launcher/ws', headers={**HEADERS, **extra}):
                    pass
        self.service.trust.secret = ''
        with self.assertRaises(WebSocketDisconnect), self.connect():
            pass

    def test_backend_can_control_launcher_without_browser(self):
        with self.connect() as ws:
            self.hello(ws)
            result = self.app.state.gateway.router.dispatch('launcher.autostart.set', {'enabled': True}, local=True)
            command = ws.receive_json()
            LauncherCommand.model_validate(command)
            self.assertEqual(command['method'], 'autostart.set')
            self.assertEqual(command['id'], result['commandId'])
            self.assertEqual(command['params'], {'enabled': True})
            ws.send_json({'type': 'response', 'id': command['id'], 'ok': True,
                          'result': {'supported': True, 'enabled': True}})
            self.assertTrue(call(ws, 'system.ping')['ok'])
            self.assertEqual(self.service.command(command['id'])['status'], 'succeeded')
            self.assertTrue(self.service.status()['autostart']['enabled'])

    def test_invalid_and_wrong_direction_methods_keep_connection_usable(self):
        with self.connect() as ws:
            self.assertEqual(call(ws, 'login.issue')['error']['code'], 'UNAUTHORIZED')
            self.hello(ws)
            self.assertEqual(call(ws, 'autostart.set', {'enabled': True})['error']['code'], 'METHOD_NOT_FOUND')
            self.assertEqual(call(ws, 'state.report', {'autostart': {'supported': 'yes'}})['error']['code'], 'INVALID_PARAMS')
            self.assertTrue(call(ws, 'system.ping', request_id='once')['ok'])
            self.assertEqual(call(ws, 'system.ping', request_id='once')['error']['code'], 'DUPLICATE_REQUEST')
            ws.send_text('not JSON')
            self.assertEqual(ws.receive_json()['error']['code'], 'INVALID_PARAMS')
            self.assertTrue(call(ws, 'system.ping', request_id='still-alive')['ok'])

    def test_late_results_do_not_change_expired_command_state(self):
        with self.connect() as ws:
            self.hello(ws)
            command_id = self.service.set_autostart(True)['commandId']
            ws.receive_json()
            self.service.commands[command_id]['deadline'] = time.time() - 1
            ws.send_json({'type': 'response', 'id': command_id, 'ok': True,
                          'result': {'supported': True, 'enabled': True}})
            call(ws, 'system.ping')
            self.assertEqual(self.service.command(command_id)['status'], 'expired')
            self.assertFalse(self.service.status()['autostart']['enabled'])

    def test_disconnect_fails_pending_commands_and_replacement_survives_old_cleanup(self):
        with self.connect() as first:
            self.hello(first)
            command_id = self.service.set_autostart(True)['commandId']
            first.receive_json()
            with self.connect() as second:
                self.hello(second)
                self.assertEqual(self.service.command(command_id)['status'], 'failed')
                self.assertTrue(call(second, 'system.ping')['ok'])
                self.assertTrue(self.service.status()['connected'])
        self.assertFalse(self.service.status()['connected'])

    def test_notifications_follow_hello_and_replay_until_acknowledged(self):
        self.service.publish({'instance': 'pilot', 'title': 'notice', 'content': 'body'})
        with self.connect() as ws:
            self.hello(ws)
            event = ws.receive_json()
            LauncherEvent.model_validate(event)
            self.assertEqual(event['topic'], 'notification')
        with self.connect() as ws:
            self.hello(ws)
            repeated = ws.receive_json()
            self.assertEqual(repeated['id'], event['id'])
            self.assertTrue(call(ws, 'notifications.ack', {'ids': [event['id']]})['ok'])
        self.assertEqual(len(self.service.notifications), 0)

    def test_notification_and_command_buffers_are_bounded(self):
        for n in range(110):
            self.service.publish({'instance': 'pilot', 'title': str(n), 'content': ''})
        self.assertEqual(len(self.service.notifications), 100)
        self.assertEqual(next(iter(self.service.notifications.values()))['data']['title'], '10')
        self.service.notifications.clear()
        with self.connect() as ws:
            self.hello(ws)
            for _ in range(32):
                self.service.set_autostart(True)
            with self.assertRaises(ApiError) as error:
                self.service.set_autostart(True)
            self.assertEqual(error.exception.code, 'BUSY')

    def test_browser_gets_one_time_login_without_access_password(self):
        with self.connect() as launcher:
            self.hello(launcher)
            issued = call(launcher, 'login.issue')
            self.assertNotIn('private-password', json.dumps(issued))
            ticket = issued['result']['ticket']
            with self.client.websocket_connect('/api/v1/ws', headers={'host': '127.0.0.1:25548'}) as browser:
                browser.receive_json()
                for request_id, expected in [('first', True), ('second', False)]:
                    browser.send_json({'v': 1, 'type': 'request', 'id': request_id,
                                       'method': 'auth.launcher', 'params': {'ticket': ticket}})
                    reply = browser.receive_json()
                    self.assertEqual(reply['ok'], expected, reply)

    def test_browser_rpc_is_local_and_demo_write_protected(self):
        router = self.app.state.gateway.router
        with self.assertRaises(ApiError) as error:
            router.dispatch('launcher.status', {})
        self.assertEqual(error.exception.code, 'LOCAL_ONLY')
        with patch.dict('os.environ', {'DEMO': '1'}), self.assertRaises(ApiError) as error:
            router.dispatch('launcher.autostart.set', {'enabled': True}, local=True)
        self.assertEqual(error.exception.code, 'READ_ONLY')

    def test_applications_do_not_share_connections_or_tickets(self):
        with tempfile.TemporaryDirectory() as directory:
            other = create_app(root=fixture(directory), password='other', manage_runtime=False, mount_mcp=False)
            with self.connect() as ws:
                self.hello(ws)
                ticket = call(ws, 'login.issue')['result']['ticket']
                other_service = other.state.gateway.router.launcher
                self.assertFalse(other_service.status()['connected'])
                self.assertFalse(other_service.trust.consume(ticket))

    def test_old_endpoints_are_removed(self):
        for path in ['/api/launcher/status', '/api/launcher/startup', '/api/launcher/stream',
                     '/api/launcher/report', '/api/launcher/trusted-login', '/api/notify',
                     '/api/notify_stream', '/launcher-login']:
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_remote_browser_cannot_use_launcher_ticket(self):
        ticket = self.service.trust.issue()['ticket']
        remote = TestClient(self.app, client=('203.0.113.1', 50000))
        with remote.websocket_connect('/api/v1/ws') as ws:
            ws.receive_json()
            ws.send_json({'v': 1, 'type': 'request', 'id': 'login', 'method': 'auth.launcher',
                          'params': {'ticket': ticket}})
            self.assertFalse(ws.receive_json()['ok'])
        self.assertTrue(self.service.trust.consume(ticket))

    def test_binary_frames_are_rejected(self):
        with self.connect() as ws:
            ws.send_bytes(b'binary')
            self.assertEqual(ws.receive()['code'], 1003)
