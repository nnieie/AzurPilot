"""验证单 SHA 构建产物可供本地客户端完成更新。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from deploy.git_over_cdn.client import GitOverCdnClient


ROOT = Path(__file__).resolve().parents[1]
BUILDER = (ROOT / '.github/scripts/build_git_over_cdn_eo_esa.mjs').as_uri()


def git(cwd, *args, data=None):
    return subprocess.run(
        ['git', *args], cwd=cwd, input=data, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class TestGitOverCdnBuild(unittest.TestCase):
    def test_single_sha_build_and_client_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local, mirror = (root / name for name in ('source', 'local', 'mirror.git'))
            git(root, 'init', str(source))
            stream = bytearray()
            for index in range(5):
                message = f'测试提交 {index}\n'.encode('utf-8')
                content = f'文件内容 {index}\n'.encode('utf-8')
                stream.extend(
                    f'commit refs/heads/master\nmark :{index + 1}\n'
                    f'committer 测试 <test@example.invalid> {1700000000 + index} +0800\n'
                    f'data {len(message)}\n'.encode('utf-8') + message
                )
                if index:
                    stream.extend(f'from :{index}\n'.encode())
                stream.extend(f'M 100644 inline file.txt\ndata {len(content)}\n'.encode() + content + b'\n')
            git(source, 'fast-import', '--quiet', data=bytes(stream))
            old = git(source, 'rev-parse', 'master~2').decode()
            latest = git(source, 'rev-parse', 'master').decode()
            git(source, 'update-ref', 'refs/heads/master', old)
            git(root, 'clone', '--no-local', str(source), str(local))
            git(source, 'update-ref', 'refs/heads/master', latest)
            git(root, 'init', '--bare', str(mirror))
            git(source, 'push', '--force', str(mirror), 'HEAD:refs/heads/master')
            self.assertEqual(latest, git(mirror, 'rev-parse', 'master').decode())

            output = root / 'output'
            options = dict(
                branch='master', ref=latest, history=3, output=str(output), fetch=False,
                remote='origin', siteUrl='https://cdn.example/', mirrorUrls=['https://cdn.example/'],
            )
            script = (
                f'import {{ buildStaticFiles }} from {json.dumps(BUILDER)};\n'
                'await buildStaticFiles(JSON.parse(process.argv[2]), process.argv[1]);'
            )
            result = subprocess.run(
                ['node', '--input-type=module', '-e', script, str(source), json.dumps(options)],
                capture_output=True,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', errors='replace'))
            self.assertEqual({'commit': latest}, json.loads((output / 'latest.json').read_text()))
            self.assertEqual(3, len(list(output.glob('*/*.zip'))))
            self.assertEqual({latest}, {file.parent.name for file in output.glob('*/*.zip')})
            self.assertFalse(list(output.glob('*/pack-*')))

            class StaticSession:
                def get(self, url, **kwargs):
                    relative = urlparse(url).path.lstrip('/')
                    body = (output / relative).read_bytes()
                    return SimpleNamespace(status_code=200, text=body.decode() if relative.endswith('.json') else '', content=body)

            stale = git(local, 'rev-parse', 'HEAD^').decode()
            git(local, 'update-ref', 'refs/remotes/origin/master', stale)
            git(local, 'pack-refs', '--all')
            client = GitOverCdnClient('https://cdn.example', str(local))
            client.preferred_urls = client.urls
            client.session = StaticSession()
            self.assertEqual('behind', client.get_status())
            self.assertTrue(client.update())
            self.assertEqual(latest, git(local, 'rev-parse', 'HEAD').decode())
            self.assertEqual('uptodate', client.get_status())
            self.assertEqual(git(source, 'rev-parse', 'HEAD^{tree}'), git(local, 'rev-parse', 'HEAD^{tree}'))
            git(local, 'fsck', '--strict')


if __name__ == '__main__':
    unittest.main()
