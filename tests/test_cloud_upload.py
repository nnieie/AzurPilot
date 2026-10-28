"""Disk outbox retries and cloud/local recording coexistence; no real uploads."""
import hashlib
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


class DirectSession(UploadSession):
    """Answers A's v2 control calls; presigned parts are completed already."""
    def post(self, url, **kwargs):
        if self.fail:
            raise requests.ConnectionError('offline')
        self.calls.append((url, kwargs))
        if url.endswith('/init'):
            self.upload_id = kwargs['json']['metadata']['upload_id']
        payload = {'schema_version': 2, 'accepted': True, 'upload_id': self.upload_id}
        if '/prepare' in url or url.endswith('/complete'):
            payload['completed'] = True
        if url.endswith('/complete') and '/files/' not in url:
            payload['sha256'] = 'f' * 64
        return Mock(status_code=202, json=Mock(return_value=payload))


class CloudUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'queue'
        self.meta = {'device_id': 'a' * 32, 'instance_id': 'b' * 16, 'project': 'research',
                     'scene': 'research_complete', 'server': 'cn', 'collected_at': '2026-10-27T06:00:00+00:00'}
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)

    def test_legacy_v1_queue_is_upgraded_to_direct_upload_and_keeps_one_id(self):
        queue = CloudUploadQueue(self.root, protocol_version=1)
        ident = queue.enqueue([self.image, self.image], self.meta)
        failed = DirectSession()
        failed.fail = True
        queue.drain_once(failed)
        entries = list(self.root.iterdir())
        self.assertEqual(len(entries), 1)
        job = entries[0]
        upgraded = json.loads((job / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(upgraded['upload_version'], 2)
        self.assertEqual(len(upgraded['files']), 2)
        self.assertNotEqual(upgraded['upload_id'], ident)
        self.assertEqual(job.name, f"{upgraded['upload_id']}.pending")

        session = DirectSession()
        queue.send(job, session)
        self.assertEqual(session.upload_id, upgraded['upload_id'])
        self.assertTrue(all('/api/v2/uploads/' in url for url, _ in session.calls))
        init = next(kwargs['json'] for url, kwargs in session.calls if url.endswith('/init'))
        self.assertEqual(init['schema_version'], 2)
        self.assertEqual([item['sha256'] for item in init['files']],
                         [hashlib.sha256((job / f'{index}.png').read_bytes()).hexdigest() for index in range(2)])

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
                queue = CloudUploadQueue(self.root, protocol_version=1)
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

    def test_direct_upload_streams_raw_png_to_presigned_part(self):
        seen = []
        state = {}
        parts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                seen.append(self.path)
                upload_id = state.get('metadata', {}).get('upload_id')
                if self.path.endswith('/init'):
                    state.update(json.loads(body))
                    upload_id = state['metadata']['upload_id']
                    payload = {'schema_version': 2, 'accepted': True, 'upload_id': upload_id}
                elif self.path.endswith('/prepare'):
                    index = int(self.path.split('/')[-2])
                    size = state['metadata']['file_sizes'][index]
                    payload = {'schema_version': 2, 'accepted': True, 'upload_id': upload_id,
                               'parts': [{'number': 1, 'offset': 0, 'size': size,
                                          'url': f'https://upload.invalid/part/{index}'}]}
                elif '/files/' in self.path:
                    payload = {'schema_version': 2, 'accepted': True, 'upload_id': upload_id, 'completed': True}
                else:
                    payload = {'schema_version': 2, 'accepted': True, 'upload_id': upload_id,
                               'completed': True, 'sha256': 'f' * 64}
                data = json.dumps(payload).encode()
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class FakeStorage:
            def __init__(self):
                self.headers = {}
                self.auth = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def put(self, url, data=None, headers=None, timeout=None, allow_redirects=None):
                parts.append((url, headers.get('Content-Type'), data.read()))
                return Mock(status_code=200)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            queue = CloudUploadQueue(self.root, base_url=f'http://127.0.0.1:{server.server_port}/api/v2/uploads',
                                     protocol_version=2)
            ident = queue.enqueue([self.image], self.meta)
            with requests.Session() as session:
                with patch.object(cloud_upload.requests, 'Session', FakeStorage):
                    queue.drain_once(session)
            self.assertEqual(len(seen), 4)
            self.assertTrue(seen[0].endswith('/init'))
            self.assertTrue(seen[1].endswith('/prepare'))
            self.assertEqual(seen[2], f'/api/v2/uploads/{ident}/files/0/complete')
            self.assertEqual(seen[3], f'/api/v2/uploads/{ident}/complete')
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0][1], 'application/octet-stream')
            self.assertEqual(parts[0][2][:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(len(parts[0][2]), state['metadata']['file_sizes'][0])
            self.assertEqual(state['files'][0]['sha256'], hashlib.sha256(parts[0][2]).hexdigest())
            self.assertFalse(list(self.root.iterdir()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
