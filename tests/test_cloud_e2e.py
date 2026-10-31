"""Opt-in, isolated PostgreSQL + template API + Worker + main API integration.

The launcher sets AZURPILOT_CLOUD_E2E to local JSON connection settings. Synthetic
icons must never be uploaded to production. Run through dev_tools/cloud_api_e2e.py.
"""

import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import cv2
import pytest
from websockets.sync.client import connect

from module.cloud.protocol import PublicAPI
from module.cloud.service import CloudService
from module.persistence.database import BusinessDatabase
from module.statistics.cl1_database import Cl1Database

SETTINGS = os.environ.get('AZURPILOT_CLOUD_E2E')
pytestmark = pytest.mark.skipif(not SETTINGS, reason='Requires isolated local cloud services')


def test_client_complete_cloud_cycle(tmp_path):
    settings = json.loads(Path(SETTINGS).read_text(encoding='utf-8'))
    assert all(settings[key].startswith('http://127.0.0.1:') for key in ('main', 'templates', 'cdn'))
    socket = connect(settings['templates'].replace('http:', 'ws:') + '/internal/v1/templates/ws',
                     origin='https://admin.azurpilot.com', proxy=None)
    socket.send(json.dumps({'type': 'auth', 'token': settings['token']}))
    assert json.loads(socket.recv())['type'] == 'authenticated'
    serial = 0

    def rpc(method, params=None):
        nonlocal serial
        serial += 1
        identity = str(serial)
        socket.send(json.dumps({'id': identity, 'method': method, 'params': params or {}}))
        while True:
            response = json.loads(socket.recv(timeout=10))
            if response.get('id') == identity:
                assert response['type'] == 'result', response
                return response['data']

    public_key = rpc('status.get')['public_key']
    database = Cl1Database(store=BusinessDatabase(tmp_path / 'local'))
    api = PublicAPI('a' * 32, **{key: settings[key] for key in ('main', 'templates', 'cdn')})
    from module.base.api_client import ApiClient
    cl1 = {'request_id': str(uuid.uuid4()), 'device_id': 'a' * 32, 'instance_id': 'b' * 16,
           'month': datetime.now(timezone.utc).strftime('%Y-%m'),
           'collected_at': datetime.now(timezone.utc).isoformat(),
           'battle_count': 2, 'akashi_encounters': 0, 'akashi_ap': 0}
    with patch.object(ApiClient, 'PRIMARY_DOMAIN', settings['main']), \
            patch.object(ApiClient, 'FALLBACK_DOMAIN', settings['main']):
        assert ApiClient._submit_cl1_data(cl1, 10)
        assert ApiClient._submit_cl1_data(cl1, 10)  # Duplicate business acknowledgement.
    queue = CloudService(tmp_path / 'queue', 'a' * 32, api=api, public_key=public_key,
                         apply_mappings=lambda mappings: database.apply_template_mappings(mappings, instance='test'))
    queue.start = Mock()  # The test controls each durable worker pass.
    sources = []
    for seed, amount in [(1234, 2), (4321, 3)]:
        image = np.random.default_rng(seed).integers(20, 230, (96, 96, 3), dtype=np.uint8)
        source = queue.identify(image, '1', scene='research')
        assert source['item'].startswith('U?')
        sources.append({**source, 'amount': amount})
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    observations = []
    for index, seed in enumerate((1234, 4321)):
        x = 100 + index * 110
        frame[200:296, x:x + 96] = np.random.default_rng(seed).integers(20, 230, (96, 96, 3), dtype=np.uint8)
        observations.append({'source': sources[index]['source_template_id'], 'bounds': [x, 200, x + 96, 296]})
    queue.capture(frame, observations, scene='research')
    database.add_research_drop('test', 'D-737-MI', 9,
                               {row['item']: row['amount'] for row in sources},
                               imgid='first', source_items=sources)
    stamp = datetime.now(timezone.utc).isoformat()
    for index in range(51):
        queue.enqueue({'record_id': f'client-e2e-{index:04d}', 'scene': 'commission', 'server': 'cn',
                       'status': 0, 'comm': 'Client API E2E', 'name': '客户端接口闭环',
                       'collected_at': stamp, 'items': sources})
        queue.enqueue({'record_id': f'client-research-e2e-{index:04d}', 'scene': 'research', 'server': 'cn',
                       'status': 0, 'series': 9, 'project': 'D-737-MI',
                       'collected_at': stamp, 'items': sources})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        queue._next_attempt = 0
        queue.drain_once()
        if len(queue._bindings()) == 2 and not list((queue.root / 'jobs').glob('*.json')):
            break
        time.sleep(0.2)
    else:
        raise AssertionError('Initial durable registrations and drop acknowledgements timed out')
    bindings = queue._bindings()
    assert not list((queue.root / 'screenshots').glob('*.json'))
    for observation in observations:
        identity = bindings[observation['source']]['identity_id']
        captures = rpc('screenshots.list', {'identity_id': identity})['items']
        assert len(captures) == 1 and captures[0]['bounds'] == observation['bounds']
        assert (captures[0]['width'], captures[0]['height']) == (1280, 720)
    screenshot = bytearray()
    while True:
        chunk = rpc('screenshots.get', {'sha256': captures[0]['sha256'], 'offset': len(screenshot)})
        screenshot.extend(base64.b64decode(chunk['data_base64']))
        if len(screenshot) == chunk['total_size']:
            break
    expected = cv2.imencode('.png', cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))[1].tobytes()
    assert bytes(screenshot) == expected
    unknowns = {row['template_id'] for row in bindings.values()}
    assert len(unknowns) == 2 and all(name.startswith('U-') for name in unknowns)
    candidates = []
    for seed in (1234, 4321):
        cell = np.random.default_rng(seed).integers(20, 230, (96, 96, 3), dtype=np.uint8)
        icon = cv2.cvtColor(cell[21:70, 40:89], cv2.COLOR_RGB2BGR)
        candidates.append({'image_base64': base64.b64encode(cv2.imencode('.png', icon)[1]).decode(),
                           'client_revision': queue.catalog.revision, 'scene': 'commission', 'client': 'AzurPilot'})
    assert {row['template_id'] for row in api.register_many(candidates)['results']} == unknowns
    assert not list((queue.root / 'jobs').glob('*.json'))
    assert set(database.get_research_drop('test')[0]['items']) == unknowns
    # The main server recognizes retries using the exact persisted record ID.
    retry = {'record_id': 'client-e2e-0000', 'scene': 'commission', 'server': 'cn',
             'status': 0, 'comm': 'Client API E2E', 'collected_at': stamp,
             'items': [{'item': next(iter(unknowns)), 'amount': 5, 'tag': ''}]}
    assert api.submit([retry])['results'][0]['status'] == 'duplicate'
    for unknown in unknowns:
        rpc('mappings.save', {'unknown_id': unknown, 'item_id': 'BlueprintClientE2E_2',
                              'name': '客户端测试图纸', 'tag': ''})
    rpc('publish', {'note': 'Isolated client API integration'})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        queue._next_attempt = queue._next_sync = 0
        queue.drain_once()
        local = database.get_research_drop('test')[0]
        if local['items'] == {'BlueprintClientE2E_2': 5}:
            report = api.statistics('commission', server='cn', comm='Client API E2E')
            if report.get('groups') and report['groups'][0]['items'][0]['item'] == 'BlueprintClientE2E_2':
                break
        time.sleep(0.2)
    else:
        raise AssertionError('Published mapping did not reach local and main statistics')
    group = report['groups'][0]
    assert group['samples'] == 51
    assert group['items'][0]['amount'] == 255
    assert group['items'][0]['occurrences'] == 51
    research = api.statistics('research', server='cn', series=9, project='D-737-MI')['groups'][0]
    assert research['samples'] == 51
    assert research['items'][0]['amount'] == 255
    assert research['items'][0]['occurrences'] == 51
    for scene in ('commission', 'research'):
        options = api.statistics(scene, options=True, server='cn')
        assert options['schema_version'] == 3 and options['options']
    # Original IDs survive both server mapping and a client restart.
    assert local['source_items'] == sources
    restored = CloudService(queue.root, 'a' * 32, api=api, public_key=public_key,
                            apply_mappings=lambda mappings: database.apply_template_mappings(mappings, instance='test'))
    assert restored.catalog.revision == queue.catalog.revision
    assert len(restored.catalog.templates) == 2
    assert {row['template_id'] for row in restored.catalog.templates} == {'BlueprintClientE2E_2'}
    # New records collected after a merge must still identify their original
    # template, otherwise a later correction cannot separate these quantities.
    cell = np.random.default_rng(4321).integers(20, 230, (96, 96, 3), dtype=np.uint8)
    after_mapping = restored.identify(cell, '1')
    assert after_mapping['item'] == 'BlueprintClientE2E_2'
    assert after_mapping['source_template_id'] == bindings[sources[1]['item']]['identity_id']
    restored.start = Mock()
    for scene in ('commission', 'research'):
        restored.enqueue({'record_id': 'client-after-mapping-' + scene, 'scene': scene, 'server': 'cn',
                          'status': 0, 'comm': 'Client API E2E', 'name': '客户端接口闭环',
                          'series': 9, 'project': 'D-737-MI', 'collected_at': stamp,
                          'items': [{**after_mapping, 'amount': 3}]})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        restored._next_attempt = 0
        restored.drain_once()
        if not list((restored.root / 'jobs').glob('*.json')):
            break
        time.sleep(0.2)
    else:
        raise AssertionError('Post-mapping records were not acknowledged')
    # A correction separates the old identities again without changing their total.
    rpc('mappings.save', {'unknown_id': bindings[sources[1]['item']]['identity_id'],
                          'item_id': 'BlueprintCorrected', 'name': '修正图纸', 'tag': 'bonus'})
    rpc('publish', {'note': 'Verify corrected mapping preserves provenance'})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        restored._next_sync = restored._next_attempt = 0
        restored.drain_once()
        local = database.get_research_drop('test')[0]
        if local['items'] == {'BlueprintClientE2E_2': 2, 'BlueprintCorrected': 3}:
            expected = {('BlueprintClientE2E_2', ''): 102, ('BlueprintCorrected', 'bonus'): 156}
            expected_occurrences = {('BlueprintClientE2E_2', ''): 51, ('BlueprintCorrected', 'bonus'): 52}
            corrected = [api.statistics(scene, server='cn')['groups'][0] for scene in ('commission', 'research')]
            if all({(row['item'], row['tag']): row['amount'] for row in group['items']} == expected
                   and group['samples'] == 52
                   and {(row['item'], row['tag']): row['occurrences'] for row in group['items']} == expected_occurrences
                   for group in corrected):
                break
        time.sleep(0.2)
    else:
        raise AssertionError('Corrected mapping did not recover original local quantities')
    assert sorted((row['item'], row['tag'], row['amount']) for row in local['item_entries']) == [
        ('BlueprintClientE2E_2', '', 2), ('BlueprintCorrected', 'bonus', 3)]
    report = {'revision': restored.catalog.revision, 'events': 104, 'commission_samples': 52,
              'research_samples': 52, 'amount_per_scene': 258,
              'template_variants': 2, 'historical_correction': True,
              'post_mapping_source_identity_preserved': True,
              'cl1_post_and_duplicate_verified': True,
              'batch_template_registration_verified': True,
              'direct_template_upload': True, 'signed_worker_distribution': True,
              'private_full_screenshots_verified': True}
    Path('log/cloud-api-e2e-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    socket.close()
