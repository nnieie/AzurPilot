"""v2 client-to-cloud streaming and retained v1 outbox compatibility."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from module.statistics.cloud_upload import CloudUploadQueue, _FileSlice


def reply(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


class DirectControl:
    def __init__(self):
        self.calls = []
        self.completed = set()
        self.received = {}
        self.generation = 0
        self.fail_storage = False
        self.bad_url = None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/init'):
            self.manifest = kwargs['json']
            assert self.manifest['schema_version'] == 2
            self.id = self.manifest['metadata']['upload_id']
        body = {'accepted': True, 'schema_version': 2, 'upload_id': self.id}
        if url.endswith('/prepare'):
            index = int(url.split('/')[-2])
            self.generation += 1
            if index in self.completed:
                body['completed'] = True
            else:
                size = self.manifest['metadata']['file_sizes'][index]
                half = size // 2
                body.update(completed=False, parts=[
                    {'number': n + 1, 'offset': offset, 'size': length,
                     'url': self.bad_url or f'https://uploads.123624.com/{index}/{n + 1}?generation={self.generation}'}
                    for n, (offset, length) in enumerate([(0, half), (half, size - half)])])
                self.received[index] = []
        elif '/files/' in url and url.endswith('/complete'):
            index = int(url.split('/')[-2])
            payload = b''.join(self.received[index])
            assert hashlib.md5(payload).hexdigest() == self.manifest['files'][index]['md5']
            self.completed.add(index)
            body['completed'] = True
        elif url.endswith('/complete'):
            assert len(self.completed) == len(self.manifest['files'])
            body['sha256'] = 'a' * 64
        return reply(body, 202 if url.endswith('/init') else 200)


class StorageSession:
    def __init__(self, control, seen):
        self.control, self.seen = control, seen
        # These defaults must be cleared before making a signed PUT.
        self.headers = {'Authorization': 'must-not-leak', 'Platform': 'must-not-leak'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def put(self, url, data, headers, **kwargs):
        assert not self.headers
        assert set(headers) == {'Content-Type', 'Content-Length'}
        assert kwargs['allow_redirects'] is False
        if self.control.fail_storage:
            return reply({}, 403)
        chunks = []
        while block := data.read(1 << 20):
            assert len(block) <= 64 << 10
            chunks.append(block)
        payload = b''.join(chunks)
        assert len(payload) == int(headers['Content-Length'])
        index = int(url.split('/')[-2])
        self.control.received[index].append(payload)
        self.seen.append((url, payload))
        return reply({})


class DirectUploadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'queue'
        self.meta = {'device_id': 'a' * 32, 'instance_id': 'b' * 16, 'project': 'research',
                     'scene': 'research_complete', 'server': 'cn', 'collected_at': '2026-10-27T06:00:00+00:00'}
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.storage = []
        self.control = DirectControl()

    def storage_session(self):
        return StorageSession(self.control, self.storage)

    def test_v2_uploads_only_metadata_to_a_and_parts_to_cloud(self):
        queue = CloudUploadQueue(self.root)
        ident = queue.enqueue([self.image, self.image], self.meta)
        manifest = json.loads((self.root / f'{ident}.pending' / 'metadata.json').read_text())
        self.assertEqual(manifest['upload_version'], 2)
        with patch('module.statistics.cloud_upload.requests.Session', self.storage_session):
            queue.drain_once(self.control)
        self.assertFalse(list(self.root.iterdir()))
        self.assertEqual(len(self.storage), 4)
        self.assertTrue(all('/api/v2/uploads/' in url for url, _ in self.control.calls))
        self.assertTrue(all('data' not in kwargs for _, kwargs in self.control.calls))
        self.assertEqual(self.control.manifest['metadata']['collected_at'], self.meta['collected_at'])
        self.assertNotIn('upload_version', self.control.manifest['metadata'])
        self.assertNotIn('files', self.control.manifest['metadata'])

    def test_expired_storage_url_keeps_job_then_refreshes_after_restart(self):
        queue = CloudUploadQueue(self.root)
        ident = queue.enqueue([self.image], self.meta)
        self.control.fail_storage = True
        with patch('module.statistics.cloud_upload.requests.Session', self.storage_session):
            queue.drain_once(self.control)
            self.assertTrue((self.root / f'{ident}.pending').exists())
            generation = self.control.generation
            self.control.fail_storage = False
            CloudUploadQueue(self.root).drain_once(self.control)
        self.assertGreater(self.control.generation, generation)
        self.assertFalse(list(self.root.iterdir()))

    def test_bad_storage_destinations_rejected_before_any_image_transfer(self):
        for url in ['http://uploads.123624.com/part', 'https://user:pass@uploads.123624.com/part',
                    'https://uploads.123624.com/part#fragment', 'file:///tmp/image']:
            with self.subTest(url=url):
                queue = CloudUploadQueue(self.root)
                ident = queue.enqueue([self.image], self.meta)
                self.control.bad_url = url
                with patch('module.statistics.cloud_upload.requests.Session', self.storage_session):
                    with self.assertRaises(ValueError):
                        queue.send(self.root / f'{ident}.pending', self.control)
                self.assertFalse(self.storage)
                queue._remove(self.root / f'{ident}.pending')

    def test_same_size_changed_file_is_rejected_before_contacting_a(self):
        queue = CloudUploadQueue(self.root)
        ident = queue.enqueue([self.image], self.meta)
        job = self.root / f'{ident}.pending'
        path = job / '0.png'
        image = path.read_bytes()
        path.write_bytes(image[:-1] + bytes([image[-1] ^ 1]))
        with self.assertRaises(ValueError):
            queue.send(job, self.control)
        self.assertFalse(self.control.calls)

    def test_file_slices_have_exact_lengths_and_bounded_reads(self):
        queue = CloudUploadQueue(self.root)
        queue.root.mkdir()
        path = self.root / 'large'
        path.write_bytes(b'x' * (200 << 10))
        with path.open('rb') as f:
            one, two = _FileSlice(f, 100 << 10, queue), None
            chunks = []
            while block := one.read():
                chunks.append(block)
            self.assertEqual(sum(map(len, chunks)), 100 << 10)
            self.assertLessEqual(max(map(len, chunks)), 64 << 10)
            two = _FileSlice(f, 100 << 10, queue)
            self.assertEqual(two.tell(), 0)
            self.assertEqual(len(two), 100 << 10)
            self.assertEqual(len(two.read(100)), 100)
            self.assertEqual(two.tell(), 100)


if __name__ == '__main__':
    unittest.main()
