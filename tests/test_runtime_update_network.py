"""Runtime CDN updates do not change the bootstrap downloader or the working tree on fetch."""

import io
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import niquests
import pytest
import requests

from deploy.git_over_cdn.client import GitOverCdnClient
from module.base import http
from module.runtime.update_network import RuntimeGitManager, RuntimeGitOverCdnClient


def test_runtime_cdn_uses_browser_headers_without_changing_deploy():
    with patch('module.runtime.update_network._read_device_id', return_value='test-device'):
        session = RuntimeGitOverCdnClient._create_session(max_retries=0)
    with session, patch.object(niquests.Session, 'request', return_value=None) as request:
        session.get('https://update.azurpilot.com/latest.json')
    headers = request.call_args.kwargs['headers']
    assert headers['User-Agent'] == http.BROWSER_USER_AGENT
    assert 'Origin' not in headers
    assert session.trust_env is False
    assert session.headers['X-Device-Id'] == 'test-device'
    assert session.headers['Cloudflare-Workers-Version-Key'] == 'test-device'
    with GitOverCdnClient._create_session(max_retries=0) as bootstrap:
        assert isinstance(bootstrap, requests.Session)
        assert not isinstance(bootstrap, niquests.Session)


@pytest.mark.parametrize('enabled', [True, False])
def test_policy_uses_runtime_api_headers(enabled):
    response = MagicMock(text=json.dumps(enabled))
    response.__enter__.return_value = response
    response.json.return_value = {'schema_version': 2, 'force_update': enabled}
    with patch.object(http, 'get', return_value=response) as get:
        assert RuntimeGitManager.cloud_force_update_enabled() is enabled
    assert get.call_args.args[0] == 'https://api.azurpilot.com/api/v2/get/update/data.json'
    assert get.call_args.kwargs['headers']['User-Agent'] == 'AzurPilot/2.1'


@pytest.mark.parametrize('policy', [None, {}, {'schema_version': 1, 'force_update': True},
                                   {'schema_version': 2, 'force_update': 'true'}, 1])
def test_invalid_policy_does_not_enable_force_update(policy):
    response = MagicMock()
    response.__enter__.return_value = response
    response.json.return_value = policy
    with patch.object(http, 'get', return_value=response):
        assert RuntimeGitManager.cloud_force_update_enabled() is None


def test_policy_network_failure_does_not_block_updates():
    with patch.object(http, 'get', side_effect=http.exceptions.Timeout('timeout')):
        assert RuntimeGitManager.cloud_force_update_enabled() is None


@pytest.mark.parametrize('operation', ['fetch', 'update'])
@pytest.mark.parametrize('cdn_enabled, cdn_success', [(True, True), (True, False), (False, False)])
def test_runtime_update_closes_cdn_and_falls_back_to_git(operation, cdn_enabled, cdn_success):
    manager = Mock(spec=RuntimeGitManager)
    manager.GitOverCdn = cdn_enabled
    manager.Branch = 'master'
    client = manager.goc_client
    getattr(client, operation).return_value = cdn_success
    if operation == 'fetch':
        RuntimeGitManager.fetch_update(manager, 'origin', 'master')
        fallback = manager._fetch_with_retry
    else:
        RuntimeGitManager.git_install(manager)
        fallback = manager.git_repository_init
    if cdn_enabled:
        getattr(client, operation).assert_called_once_with()
        client.close.assert_called_once_with()
    else:
        client.close.assert_not_called()
        getattr(client, operation).assert_not_called()
    assert fallback.called is (not cdn_enabled or not cdn_success)


def test_runtime_closes_cdn_when_update_raises():
    manager = Mock(spec=RuntimeGitManager)
    manager.GitOverCdn = True
    manager.goc_client.update.side_effect = OSError('cannot write pack')
    with pytest.raises(OSError):
        RuntimeGitManager.git_install(manager)
    manager.goc_client.close.assert_called_once_with()
    manager.git_repository_init.assert_not_called()


def test_fetch_downloads_objects_and_updates_remote_ref_without_reset(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()

    def git(root, *args, data=None):
        return subprocess.run(['git', '-C', str(root), *args], input=data,
                              check=True, capture_output=True).stdout

    for root in (source, target):
        git(root, 'init', '-b', 'master')
    parent = None
    for version in ('old', 'new'):
        (source / 'version.txt').write_text(version)
        git(source, 'add', 'version.txt')
        tree = git(source, 'write-tree').decode().strip()
        args = ('-p', parent) if parent else ()
        commit = git(source, '-c', 'user.name=Update Test', '-c', 'user.email=test@example.invalid',
                     'commit-tree', tree, *args, data=(version + '\n').encode()).decode().strip()
        git(source, 'update-ref', 'refs/heads/master', commit)
        if parent is None:
            current = commit
            pack = git(source, 'pack-objects', '--stdout', '--revs', data=(commit + '\n').encode())
            git(target, 'index-pack', '--stdin', data=pack)
            git(target, 'update-ref', 'refs/heads/master', commit)
            git(target, 'reset', '--hard', commit)
        parent = commit
    latest = parent
    prefix = source / 'release'
    pack_hash = git(source, 'pack-objects', str(prefix), '--all').decode().strip()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        for suffix in ('pack', 'idx'):
            archive.writestr(f'pack-{latest}.{suffix}', Path(f'{prefix}-{pack_hash}.{suffix}').read_bytes())
    url = 'https://update.azurpilot.com'
    manifest = Mock(status_code=200, text=json.dumps({'commit': latest, 'branch': 'master'}))
    package = Mock(status_code=200, content=payload.getvalue())
    client = RuntimeGitOverCdnClient(url=url, folder=str(target))
    client.preferred_urls = [url]
    client.logger = Mock()
    client.session = Mock()
    client.session.get.side_effect = lambda address, **kwargs: manifest if address.endswith('/latest.json') else package
    session = client.session
    try:
        assert client.fetch() is True
        assert git(target, 'rev-parse', 'origin/master').decode().strip() == latest
        assert git(target, 'rev-parse', 'HEAD').decode().strip() == current
        assert (target / 'version.txt').read_text() == 'old'
        assert session.get.call_args.args[0] == f'{url}/{latest}/{current}.zip'
    finally:
        client.close()
    session.close.assert_called_once_with()
