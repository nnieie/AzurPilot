"""可选 Rust/Python 真正互通测试；无需启动 Tauri 或接触用户配置。"""
import http.client
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import uvicorn
from module.api.app import create_app
from tests.test_api import fixture


@unittest.skipUnless(os.environ.get('AZURPILOT_LAUNCHER_TEST_BINARY'), '需指定已构建的 launcher_ws 测试可执行文件')
class LauncherInteropTests(unittest.TestCase):
    def test_rust_launcher_controls_notifications_and_login_against_python_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'AP_WEBUI_TRUST_SECRET': 'fixture-secret'}):
                app = create_app(root=fixture(directory), password='test-password',
                                 manage_runtime=False, mount_mcp=False)
            service = app.state.gateway.router.launcher
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                port = listener.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
                thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
                thread.start()
                process = None
                try:
                    env = {**os.environ, 'AZURPILOT_TEST_BACKEND_PORT': str(port)}
                    process = subprocess.Popen(
                        [os.environ['AZURPILOT_LAUNCHER_TEST_BINARY'], '--ignored', '--exact',
                         'python_backend_interop', '--nocapture'],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    )
                    deadline = time.monotonic() + 8
                    while not service.status()['connected']:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('Rust 启动器没有连接 Python 后端')
                        time.sleep(0.01)
                    command_id = service.set_autostart(True)['commandId']
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    try:
                        connection.request('POST', '/api/v2/internal/notifications',
                                           json.dumps({'instance': 'fixture', 'title': 'title', 'content': 'body'}),
                                           {'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        connection.close()
                    output, _ = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, output)
                    self.assertEqual(service.command(command_id)['status'], 'succeeded')
                    self.assertTrue(service.status()['autostart']['enabled'])
                    self.assertEqual(len(service.notifications), 0)
                    self.assertEqual(len(service.trust.tokens), 1)
                finally:
                    if process and process.poll() is None:
                        process.kill()
                        process.communicate()
                    server.should_exit = True
                    thread.join(timeout=5)
