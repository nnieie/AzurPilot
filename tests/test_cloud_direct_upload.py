"""Signed distribution, durable retry and reversible local history mapping."""

import base64
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from module.cloud.catalog import TemplateCatalog, decode_icon, icon_features, map_items
from module.cloud.files import read_json
from module.cloud.protocol import CloudError, PublicAPI
from module.cloud.service import CloudService


@pytest.fixture
def release(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    api = PublicAPI('a' * 32)
    routes = {}
    api.request = Mock(side_effect=lambda method, url, **kw: (routes[url], {'ETag': '"test"'}))

    def signed(payload):
        data = json.dumps(payload, separators=(',', ':')).encode()
        return json.dumps({'payload_base64': base64.b64encode(data).decode(),
                           'signature': base64.b64encode(key.sign(data)).decode()}).encode()

    image = np.random.default_rng(42).integers(0, 255, (49, 49, 3), np.uint8)
    png = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tobytes()
    digest = hashlib.sha256(png).hexdigest()

    def publish(revision, canonical='U-1234', mappings=None, variants=1):
        url = api.cdn + f'/releases/{revision}/icons/{digest}.png'
        entries = [{'identity_id': f'U-{index}', 'template_id': canonical,
                    'kind': 'temporary' if canonical.startswith('U-') else 'formal',
                    'icon_sha256': hashlib.sha256(image.tobytes()).hexdigest(),
                    'sha256': digest, 'url': url, 'tag': ''} for index in range(variants)]
        manifest = signed({'revision': revision, 'templates': entries, 'replace_ids': mappings or []})
        manifest_url = api.cdn + f'/releases/{revision}/manifest.json'
        routes[url] = png
        routes[manifest_url] = manifest
        routes[api.cdn + '/latest.json'] = signed({'revision': revision, 'manifest_url': manifest_url,
                                                  'manifest_sha256': hashlib.sha256(manifest).hexdigest()})
        return manifest_url

    return tmp_path, public, api, routes, publish, image, signed


def test_signed_sync_variant_matching_and_restart(release):
    root, public, api, routes, publish, image, signed = release
    history = Mock()
    publish(1, 'Blueprint_2', variants=2)
    catalog = TemplateCatalog(root, api, public_key=public, apply_mappings=history)
    assert catalog.sync()
    assert catalog.revision == 1
    assert catalog.match(image, kind='formal')['template_id'] == 'Blueprint_2'
    assert len(catalog.templates) == 2
    history.assert_called_once_with([])
    restored = TemplateCatalog(root, api, public_key=public)
    assert restored.revision == 1
    assert restored.match(image, kind='formal') is not None
    publish(2, 'Blueprint_2')
    catalog.sync()
    assert len(catalog.templates) == 1


def test_tampering_and_rollback_never_activate(release):
    root, public, api, routes, publish, image, signed = release
    manifest = publish(1)
    catalog = TemplateCatalog(root, api, public_key=public)
    routes[manifest] += b' '
    with pytest.raises(CloudError, match='checksum'):
        catalog.sync()
    assert catalog.revision == 0
    publish(2)
    catalog.sync()
    publish(1)
    with pytest.raises(CloudError, match='rollback'):
        catalog.sync()
    assert catalog.revision == 2


def test_bad_signature_and_icon_checksum(release):
    root, public, api, routes, publish, image, signed = release
    publish(1)
    pointer = json.loads(routes[api.cdn + '/latest.json'])
    pointer['signature'] = base64.b64encode(b'0' * 64).decode()
    routes[api.cdn + '/latest.json'] = json.dumps(pointer).encode()
    catalog = TemplateCatalog(root, api, public_key=public)
    with pytest.raises(CloudError, match='signed'):
        catalog.sync()
    publish(1)
    icon_url = next(url for url in routes if url.endswith('.png'))
    routes[icon_url] += b'bad'
    with pytest.raises(CloudError, match='checksum'):
        catalog.sync()
    assert not (root / 'active.json').exists()


def test_journal_replays_after_history_failure(release):
    root, public, api, routes, publish, image, signed = release
    publish(1)
    history = Mock(side_effect=RuntimeError('database busy'))
    catalog = TemplateCatalog(root, api, public_key=public, apply_mappings=history)
    with pytest.raises(RuntimeError):
        catalog.sync()
    assert catalog.revision == 0
    assert (root / 'pending-release.json').exists()
    recovery = Mock()
    restored = TemplateCatalog(root, api, public_key=public, apply_mappings=recovery)
    assert restored.revision == 1
    recovery.assert_called_once()


def service(release):
    root, public, api, routes, publish, image, signed = release
    publish(1)
    queue = CloudService(root, 'a' * 32, api=api, public_key=public)
    queue.start = Mock()
    return queue, api, image


def record(number=1, items=None):
    return {'record_id': f'record-{number:04d}', 'scene': 'research', 'server': 'cn',
            'status': 0, 'series': 9, 'project': 'D-737-MI',
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'items': items or [{'item': 'Coins', 'amount': 2, 'tag': ''}]}


def test_offline_icon_is_49px_and_waits_for_server_id(release):
    queue, api, image = service(release)
    cell = np.zeros((96, 96, 3), np.uint8)
    cell[21:70, 40:89] = image
    identity = queue.identify(cell, '1')
    assert identity['item'].startswith('U?')
    icons = list((queue.root / 'icons').glob('*.json'))
    assert len(icons) == 1
    png = base64.b64decode(read_json(icons[0])['image_base64'])
    assert cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR).shape == (49, 49, 3)
    queue.enqueue(record(items=[{**identity, 'amount': 3}]))
    api.submit = Mock()
    queue._submit()
    api.submit.assert_not_called()
    api.register = Mock(return_value={'match': 'new', 'template_id': 'U-ABCD', 'identity_id': 'U-ABCD'})
    api.submit.return_value = {'schema_version': 3, 'results': [{'record_id': 'record-0001', 'status': 'accepted'}]}
    assert queue.drain_once()
    assert api.submit.call_args.args[0][0]['items'] == [{'item': 'U-ABCD', 'amount': 3, 'tag': ''}]
    assert not list((queue.root / 'jobs').glob('*.json'))


def test_partial_ack_retry_duplicate_and_rejection(release):
    queue, api, image = service(release)
    for index in range(1, 4):
        queue.enqueue(record(index))
    api.submit = Mock(return_value={'schema_version': 3, 'results': [
        {'record_id': 'record-0001', 'status': 'duplicate'},
        {'record_id': 'record-0002', 'status': 'invalid', 'error': 'bad data'}]})
    assert not queue.drain_once()
    assert [p.stem for p in (queue.root / 'jobs').glob('*.json')] == ['record-0003']
    assert (queue.root / 'rejected' / 'record-0002.json').exists()
    queue._next_attempt = 0
    api.submit.return_value = {'schema_version': 3, 'results': [{'record_id': 'record-0003', 'status': 'accepted'}]}
    assert queue.drain_once()


def test_full_icon_queue_does_not_interrupt_local_identification(release):
    queue, api, image = service(release)
    queue.MAX_JOBS = 0
    cell = np.zeros((96, 96, 3), np.uint8)
    cell[21:70, 40:89] = image
    identity = queue.identify(cell, '1')
    assert identity['item'].startswith('U?')
    assert identity['source_template_id'] == identity['item']
    assert list((queue.root / 'icons').glob('*.json')) == []


def test_pause_capacity_and_restart(release):
    queue, api, image = service(release)
    queue.enqueue(record())
    queue.enabled = lambda: False
    api.submit = Mock()
    assert queue.enqueue(record(2)) is None
    assert not queue.drain_once()
    api.submit.assert_not_called()
    queue.enabled = lambda: True
    queue.MAX_JOBS = 1
    with pytest.raises(CloudError, match='full'):
        queue.enqueue(record(2))
    with pytest.raises(ValueError):
        queue.enqueue({**record(), 'record_id': '../../escape'})
    restored = CloudService(queue.root, 'a' * 32, api=api, public_key=release[1])
    restored.start = Mock()
    api.submit.return_value = {'schema_version': 3, 'results': [{'record_id': 'record-0001', 'status': 'duplicate'}]}
    assert restored.drain_once()


def test_mapping_merges_tags_and_is_reversible():
    sources = [{'item': 'U-A', 'tag': 'bonus', 'amount': 2}, {'item': 'U-B', 'tag': 'bonus', 'amount': 3}]
    mappings = [{'unknown_id': 'U-A', 'item_id': 'Blueprint', 'tag': ''},
                {'unknown_id': 'U-B', 'item_id': 'Blueprint', 'tag': ''}]
    assert map_items(sources, mappings) == [{'item': 'Blueprint', 'tag': 'bonus', 'amount': 5}]
    mappings[1]['item_id'] = 'Other'
    assert {row['item']: row['amount'] for row in map_items(sources, mappings)} == {'Blueprint': 2, 'Other': 3}
    mappings[0]['tag'] = 'forced'
    assert map_items(sources, mappings)[0]['tag'] == 'forced'
    mappings[0]['tag'] = ''
    assert map_items(sources, mappings)[0]['tag'] == 'bonus'


def test_local_row_committed_after_registration_gets_a_durable_history_refresh(release):
    queue, api, image = service(release)
    queue.history = Mock()
    queue.enqueue(record())
    assert (queue.root / 'history-dirty.json').exists()
    queue._refresh_history()
    queue.history.assert_called_once_with([])
    assert not (queue.root / 'history-dirty.json').exists()


def test_concurrent_commit_is_not_lost_during_history_refresh(release):
    queue, api, image = service(release)
    queue.enqueue(record())
    queue.history = Mock(side_effect=lambda mappings: queue.enqueue(record(2)))
    queue._refresh_history()
    assert (queue.root / 'history-dirty.json').exists()


def test_flat_colored_icons_do_not_use_undefined_normalized_correlation(release):
    root, public, api, routes, publish, image, signed = release
    catalog = TemplateCatalog(root, api, public_key=public)
    candidate = np.full((49, 49, 3), [20, 200, 40], np.uint8)
    query = np.full((49, 49, 3), [40, 200, 40], np.uint8)
    entry = {'kind': 'temporary', 'template_id': 'U-ONE'}
    catalog.entries = [(entry, candidate, *icon_features(candidate))]
    assert catalog.match(candidate, kind='temporary') == entry
    assert catalog.match(query, kind='temporary') is None


def test_non_8bit_png_is_rejected_before_matching():
    png = cv2.imencode('.png', np.ones((49, 49, 3), np.uint16))[1].tobytes()
    with pytest.raises(CloudError):
        decode_icon(png)


def test_cdn_failure_does_not_block_known_statistics(release):
    queue, api, image = service(release)
    queue.enqueue(record())
    api.request.side_effect = CloudError('CDN unavailable')
    api.submit = Mock(return_value={'schema_version': 3, 'results': [
        {'record_id': 'record-0001', 'status': 'accepted'}]})
    assert not queue.drain_once()
    api.submit.assert_called_once()
    assert not list((queue.root / 'jobs').glob('*.json'))


def test_wire_records_keep_original_identity_and_tag_after_formal_mapping(release):
    queue, api, image = service(release)
    queue.catalog.mappings = [{'unknown_id': 'U-ORIGINAL', 'item_id': 'Blueprint_2', 'tag': 'bonus'}]
    queue.enqueue(record(items=[{'item': 'Blueprint_2', 'source_template_id': 'U-ORIGINAL',
                                 'tag': 'catchup', 'amount': 3}]))
    api.submit = Mock(return_value={'schema_version': 3, 'results': [
        {'record_id': 'record-0001', 'status': 'accepted'}]})
    queue._submit()
    assert api.submit.call_args.args[0][0]['items'] == [{'item': 'U-ORIGINAL', 'tag': 'catchup', 'amount': 3}]


def test_history_mapping_uses_existing_storage_and_keeps_instance_isolation(tmp_path):
    from module.persistence.database import BusinessDatabase
    from module.statistics.cl1_database import Cl1Database
    database = Cl1Database(store=BusinessDatabase(tmp_path))
    for instance in ['one', 'two']:
        database.add_research_drop(instance, 'D-737-MI', 9, {'U-A': 2, 'U-B': 3}, imgid='first')
    mapping = [{'unknown_id': 'U-A', 'item_id': 'Blueprint', 'tag': ''},
               {'unknown_id': 'U-B', 'item_id': 'Blueprint', 'tag': ''}]
    assert database.apply_template_mappings(mapping, instance='one') == 1
    entries = database.get_research_drop('one')
    assert entries[0]['items'] == {'Blueprint': 5}
    assert database.get_research_drop('two')[0]['items'] == {'U-A': 2, 'U-B': 3}
    mapping[1]['item_id'] = 'Other'
    assert database.apply_template_mappings(mapping, instance='one') == 1
    assert database.get_research_drop('one')[0]['items'] == {'Blueprint': 2, 'Other': 3}
    assert database.apply_template_mappings(mapping, instance='one') == 0
