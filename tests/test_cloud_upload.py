"""Disk outbox retries and cloud/local recording coexistence; no real uploads."""
import tempfile
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import requests

from module.statistics.azurstats import AzurStats
from module.statistics.cloud_upload import CloudUploadQueue
from module.statistics import cloud_upload


class UploadSession:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.upload_id = None

    def post(self, url, **kwargs):
        if url.endswith('/init'):
            self.upload_id = kwargs['json']['upload_id']
        self.calls.append((url, kwargs))
        result = {'accepted': True, 'upload_id': self.upload_id, 'sha256': 'f' * 64}
        return Mock(status_code=202, json=Mock(return_value=result))

    def put(self, url, **kwargs):
        self.calls.append((url, kwargs['data'].read()))
        if self.fail:
            raise requests.ConnectionError('offline')
        return Mock(status_code=202, json=Mock(return_value={'accepted': True, 'upload_id': self.upload_id}))


class CloudUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'queue'
        self.meta = {'device_id': 'a' * 32, 'instance_id': 'b' * 16, 'project': 'research',
                     'scene': 'research_complete', 'server': 'cn', 'collected_at': '2026-10-27T06:00:00+00:00'}
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def test_restart_reuses_job_id_and_cleans_only_after_complete(self):
        queue = CloudUploadQueue(self.root)
        ident = queue.enqueue([self.image, self.image], self.meta)
        session = UploadSession()
        session.fail = True
        queue.drain_once(session)
        self.assertTrue((self.root / f'{ident}.pending').is_dir())
        restarted = CloudUploadQueue(self.root)
        session.fail = False
        restarted.drain_once(session)
        self.assertEqual(session.upload_id, ident)
        self.assertFalse(list(self.root.iterdir()))
        self.assertTrue(session.calls[-1][0].endswith(f'/{ident}/complete'))
        manifest = next(kwargs['json'] for url, kwargs in session.calls if url.endswith('/init'))
        self.assertEqual(manifest['collected_at'], self.meta['collected_at'])
        self.assertEqual(len(manifest['file_sizes']), 2)
        self.assertTrue(all('api.azurpilot.com/api/v1/uploads/' in url for url, _ in session.calls))

    def test_quota_failure_removes_incomplete_files(self):
        queue = CloudUploadQueue(self.root, spool_bytes=1)
        with self.assertRaises(ValueError):
            queue.enqueue([self.image], self.meta)
        self.assertFalse(list(self.root.iterdir()))

    def test_disabled_cloud_keeps_pending_without_network(self):
        queue = CloudUploadQueue(self.root, enabled=lambda: False)
        queue.enqueue([self.image], self.meta)
        session = Mock()
        queue.drain_once(session)
        session.post.assert_not_called()
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_cloud_and_existing_local_statistics_both_run(self):
        config = SimpleNamespace(config_name='test', SERVER='cn', DropRecord_CloudUpload=True)
        with patch('module.statistics.cloud_upload.queue_for'):
            stat = AzurStats(config)
        with patch.object(stat, '_record_local') as local, \
                patch('module.statistics.cloud_upload.enqueue') as remote:
            stat.commit([self.image], 'opsi_daily', local=True, upload=True,
                        collected_at=self.meta['collected_at'])
        local.assert_called_once()
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[-1], self.meta['collected_at'])

    def test_queue_selection_archive_keeps_local_parsing_without_duplicate_cloud_upload(self):
        from module.research.research import RewardResearch
        config = SimpleNamespace(config_name='test', SERVER='cn', DropRecord_CloudUpload=True,
                                 DropRecord_ResearchRecord='upload')
        target = SimpleNamespace(stat=AzurStats(config), config=config,
                                 device=SimpleNamespace(image=self.image), get_queue_slot=lambda: 1,
                                 get_research_status=lambda image: ['waiting'])

        def append(drop, **kwargs):
            drop.add(self.image)
            return True

        target.research_queue_append = append
        with patch('module.statistics.cloud_upload.enqueue') as remote, \
                patch('module.statistics.research_drop.record_research_drop') as local:
            self.assertEqual(RewardResearch.research_fill_queue(target), 5)
        remote.assert_not_called()
        local.assert_called_once()

    def test_config_reload_pauses_existing_worker_without_starting_another(self):
        config = SimpleNamespace(config_name='reload-test', DropRecord_CloudUpload=True)
        queue = CloudUploadQueue(self.root)
        queue.config_name = config.config_name
        with patch.dict(cloud_upload._queues, {'instance': queue}, clear=True):
            cloud_upload.update_policy(config)
            self.assertTrue(queue.enabled())
            AzurStats(SimpleNamespace(config_name=config.config_name, DropRecord_CloudUpload=False))
            self.assertFalse(queue.enabled())
            self.assertIsNone(queue.thread)

    def test_invalid_upload_redirect_keeps_files_without_following(self):
        for target in ('file:///tmp/secret', 'http://user:pass@127.0.0.1/api/v1/uploads/init',
                       'http://127.0.0.1/other', 'https://api.azurpilot.com/api/v1/uploads/init',
                       'http://127.0.0.1:invalid/api/v1/uploads/init'):
            with self.subTest(target=target):
                queue = CloudUploadQueue(self.root)
                ident = queue.enqueue([self.image], self.meta)
                job = self.root / f'{ident}.pending'
                response = Mock(status_code=307, headers={'Location': target})
                session = Mock()
                session.post.return_value = response
                with self.assertRaises(ValueError):
                    queue.send(job, session)
                self.assertTrue(job.is_dir())
                self.assertEqual(session.post.call_count, 1)
                session.put.assert_not_called()
                response.close.assert_called_once()

    def test_real_http_redirect_sends_one_raw_png_per_put_directly_to_a(self):
        seen = []
        state = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path.endswith('/init'):
                    state.update(json.loads(body))
                seen.append((self.command, self.path, self.headers.get('X-AzurPilot-ID')))
                data = json.dumps({'accepted': True, 'upload_id': state['upload_id'], 'sha256': 'f' * 64}).encode()
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.command, self.path, self.headers.get('Content-Type'), body[:8], len(body)))
                data = json.dumps({'accepted': True, 'upload_id': state['upload_id']}).encode()
                self.send_response(202)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        redirected = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                redirected.append((self.command, self.path))
                self.send_response(307)
                self.send_header('Location', f'http://127.0.0.1:{server.server_port}{self.path}')
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_PUT = do_POST

        origin = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        try:
            queue = CloudUploadQueue(self.root, base_url=f'http://127.0.0.1:{origin.server_port}/api/v1/uploads')
            queue.enqueue([self.image], self.meta)
            with requests.Session() as session:
                queue.drain_once(session)
            self.assertEqual([call[0] for call in seen], ['POST', 'PUT', 'POST'])
            self.assertEqual(redirected, [('POST', '/api/v1/uploads/init')])
            self.assertEqual(seen[0][2], 'a' * 32)
            self.assertEqual(seen[1][2], 'image/png')
            self.assertEqual(seen[1][3], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(seen[1][4], state['file_sizes'][0])
            self.assertFalse(list(self.root.iterdir()))
        finally:
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
