"""任务通知经后端同时送至启动器 WS 和浏览器订阅。"""
import unittest
from tests import test_launcher_api

PATH = '/api/v2/internal/notifications'


class NotifyApiTests(unittest.TestCase):
    setUp = test_launcher_api.LauncherApiTests.setUp
    connect = test_launcher_api.LauncherApiTests.connect
    hello = test_launcher_api.LauncherApiTests.hello
    def test_valid_notification_reaches_launcher_and_browser(self):
        with self.connect() as launcher:
            self.hello(launcher)
            with self.client.websocket_connect('/api/v1/ws', headers={'host': '127.0.0.1:25548'}) as browser:
                browser.receive_json()
                browser.send_json({'v': 1, 'type': 'request', 'id': 'subscribe', 'method': 'events.subscribe',
                                   'params': {'topics': ['notifications']}})
                self.assertTrue(browser.receive_json()['ok'])
                payload = {'instance': 'pilot', 'title': '标题', 'content': '正文'}
                response = self.client.post(PATH, json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(launcher.receive_json()['data'], payload)
                self.assertEqual(browser.receive_json()['data'], payload)

    def test_invalid_content_and_extra_fields_are_rejected_without_enqueue(self):
        for body in ([], {'title': 2}, {'title': 'title', 'extra': 'value'}):
            self.assertEqual(self.client.post(PATH, json=body).status_code, 400)
        self.assertEqual(self.client.post(PATH, content='bad json', headers={'content-type': 'application/json'}).status_code, 400)
        self.assertEqual(self.client.post(PATH, content='text', headers={'content-type': 'text/plain'}).status_code, 400)
        self.assertEqual(len(self.service.notifications), 0)

    def test_large_notification_is_rejected(self):
        self.assertEqual(self.client.post(PATH, json={'content': 'x' * 66000}).status_code, 413)

    def test_notification_origin_and_forwarded_requests_are_rejected(self):
        for headers in ({'host': 'remote.example'}, {'Origin': 'https://evil.example'}, {'x-forwarded-for': '203.0.113.1'}):
            self.assertEqual(self.client.post(PATH, json={'title': 'x'}, headers=headers).status_code, 403)
