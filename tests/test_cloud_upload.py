"""Public v3 transport and live collection contracts replace retired screenshot protocols."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pytest

from module.cloud.protocol import CloudError, PublicAPI, retry_after
from module.statistics.commission_drop import parse_commissions, resolve_name, revise_identity
from module.statistics.cloud_upload import permitted


def test_cloud_resume_error_does_not_break_local_statistics():
    from module.statistics.azurstats import AzurStats

    config = SimpleNamespace(config_name='offline')
    with patch('module.statistics.cloud_upload.resume_pending', side_effect=CloudError('corrupt catalog')):
        stats = AzurStats(config)
    assert stats.config is config


def test_commission_queue_failure_retains_following_local_events():
    from module.statistics.commission_drop import record_commission_drops

    events = [
        {'items': [{'item': 'A', 'amount': 1, 'tag': ''}]},
        {'items': [{'item': 'B', 'amount': 2, 'tag': ''}]},
    ]
    config = SimpleNamespace(config_name='offline')
    with patch('module.statistics.commission_drop.parse_commissions', return_value=(events, [])), \
            patch('module.statistics.commission_drop.CommissionReader'), \
            patch('module.statistics.cl1_database.db.add_cloud_commission_drop') as save, \
            patch('module.statistics.cloud_upload.enqueue_record',
                  side_effect=[CloudError('queue full'), None]) as enqueue:
        assert len(record_commission_drops([], config)) == 2
    assert save.call_count == 2
    assert enqueue.call_count == 2


def test_transport_keeps_template_bytes_off_main_server():
    session = MagicMock()
    response = session.request.return_value.__enter__.return_value
    response.status_code = 200
    response.headers = {}
    response.iter_content.return_value = [b'{"template_id":"U-1234"}']
    api = PublicAPI('a' * 32, session=session)
    api.register('PNG49', 3, 'research')
    call = session.request.call_args
    assert call.args == ('POST', 'https://templates-api.azurpilot.com/api/v3/post/templates/unknown')
    assert call.kwargs['json']['image_base64'] == 'PNG49'
    assert call.kwargs['allow_redirects'] is False
    assert call.kwargs['headers']['X-AzurPilot-ID'] == 'a' * 32
    response.iter_content.return_value = [b'{"schema_version":3,"results":[]}']
    api.submit([{'record_id': '12345678', 'items': []}])
    assert session.request.call_args.args[1] == 'https://api.azurpilot.com/api/v3/post/drops'
    assert 'image_base64' not in json.dumps(session.request.call_args.kwargs['json'])


def test_optional_registration_batch_uses_template_origin_and_respects_limits():
    api = PublicAPI('a' * 32)
    with patch.object(api, 'json', return_value={'results': []}) as request:
        api.register_many([{'image_base64': 'PNG49', 'client_revision': 0}])
    assert request.call_args.args == ('POST', 'https://templates-api.azurpilot.com/api/v3/post/unknown-candidates')
    for candidates in [[], [{}] * 51, [{'image_base64': 'a' * (256 << 10)}]]:
        with pytest.raises(ValueError):
            api.register_many(candidates)


def test_source_screenshots_go_directly_to_template_service():
    api = PublicAPI('a' * 32)
    with patch.object(api, 'json', return_value={'stored': True}) as request:
        api.upload_screenshot('FULL_PNG', [{'identity_id': 'U-1234', 'bounds': [0, 0, 96, 96]}], 'commission')
    assert request.call_args.args == ('POST', 'https://templates-api.azurpilot.com/api/v3/post/templates/screenshots')
    assert request.call_args.kwargs['json']['image_base64'] == 'FULL_PNG'


@pytest.mark.parametrize('scene', ['commission', 'research'])
@pytest.mark.parametrize('options', [False, True])
def test_public_statistics(scene, options):
    api = PublicAPI('a' * 32)
    with patch.object(api, 'json', return_value={}) as request:
        api.statistics(scene, options=options, server='jp', timezone='Asia/Tokyo')
    assert request.call_args.args == ('GET', f'https://api.azurpilot.com/api/v3/get/{scene}'
                                      + ('/options' if options else ''))
    assert request.call_args.kwargs['params']['server'] == 'jp'


def test_redirect_size_and_backoff():
    session = MagicMock()
    response = session.request.return_value.__enter__.return_value
    response.headers = {'Retry-After': '120'}
    response.status_code = 429
    api = PublicAPI('a' * 32, session=session)
    with pytest.raises(CloudError) as error:
        api.submit([])
    assert error.value.retry_after == 120
    response.status_code = 302
    with pytest.raises(CloudError):
        api.submit([])
    response.status_code = 200
    response.iter_content.return_value = [b'x' * 4, b'y' * 4]
    with pytest.raises(CloudError, match='size limit'):
        api.request('GET', api.cdn + '/latest.json', limit=5)
    assert retry_after('nonsense') == 30


@pytest.mark.parametrize('url', ['https://evil.example/releases/1/x',
                                'https://templates.azurpilot.com@evil.example/releases/1/x',
                                'http://templates.azurpilot.com/releases/1/x',
                                'https://templates.azurpilot.com/releases/1/x?token=secret'])
def test_cdn_origin_cannot_be_overridden(url):
    with pytest.raises(CloudError):
        PublicAPI('a' * 32).cdn_url(url)


def test_only_supported_explicitly_enabled_scenes_upload():
    config = SimpleNamespace(DropRecord_CloudUpload=True, DropRecord_ResearchRecord='upload',
                             DropRecord_CommissionRecord='save')
    assert permitted(config, 'research')
    assert not permitted(config, 'commission')
    assert not permitted(config, 'opsi_daily')
    config.DropRecord_CloudUpload = False
    assert not permitted(config, 'research')


def test_opsi_statistics_remain_local():
    from module.statistics.azurstats import AzurStats
    config = SimpleNamespace(config_name='test', DropRecord_CloudUpload=True)
    stat = AzurStats(config)
    with patch.object(stat, '_record_local') as local, \
            patch('module.statistics.cloud_upload.enqueue_record') as remote:
        stat.commit([np.zeros((720, 1280, 3), np.uint8)], 'opsi_daily', local=True, upload=True)
    local.assert_called_once()
    remote.assert_not_called()


def test_commission_names_and_reward_disambiguation():
    assert resolve_name(' DAILY RESOURCE EXTRACTION Ⅰ ', 'en')['code'] == 'Daily Resource Extraction Ⅰ'
    assert resolve_name('BIW巡视护卫', 'cn_ios')['code'] == 'BIW Patrol Escort'
    with pytest.raises(ValueError):
        resolve_name('nonsense', 'cn')
    def item(name, amount):
        return [{'item': name, 'amount': amount}]
    assert revise_identity('Small Merchant Escort', item('Coins', 300)) == 'Small Merchant Escort N'
    assert revise_identity('BIW VIP Escort', item('Gems', 60)) == 'BIW Patrol Escort'
    assert revise_identity('Cargo Defense Ⅱ', item('Coins', 60)) == 'Cargo Defense Ⅰ'


class Reader:
    classify = staticmethod(lambda frame: frame[0])
    name = staticmethod(lambda frame: {'code': frame[1], 'name': frame[1]})
    items = staticmethod(lambda frame: frame[1])


def test_commission_pairing_placeholders_and_invalid_groups():
    items = [{'item': 'U-1234', 'source_template_id': 'U-1234', 'amount': 2, 'tag': 'bonus'}]
    events, errors = parse_commissions([
        ('done', 'Small Merchant Escort'), ('reward', items), ('exp', None),
        ('perfect', 'Cargo Defense Ⅰ'), ('ship', None),
        ('done', 'Broken'), ('reward', items), ('reward', items), ('exit', None),
        ('reward', items)], Reader())
    assert len(events) == 2
    assert events[0]['items'] == [{'item': 'U-1234', 'amount': 2, 'tag': 'bonus'}]
    assert events[1]['status'] == 1
    assert events[1]['items'][0]['item'] == 'PlaceHolder'
    assert errors == ['multiple_reward_pages', 'reward_without_commission']


def test_scrolled_research_reward_boundary_is_after_both_frames():
    from module.research.ui import ResearchUI
    from module.combat.assets import GET_ITEMS_3
    drop = SimpleNamespace(count=1, images=[], add=lambda image: drop.images.append(image))
    drop.finish_research_reward = Mock()
    device = Mock(image=np.zeros((720, 1280, 3), np.uint8))
    target = SimpleNamespace(device=device, get_items=lambda: GET_ITEMS_3)
    ResearchUI.drop_record(target, drop)
    assert len(drop.images) == 2
    drop.finish_research_reward.assert_called_once_with(1)


def test_cloud_identity_hook_keeps_canonical_suffix_and_does_not_change_other_grids():
    from module.base.button import ButtonGrid
    from module.statistics.item import ItemGrid
    grids = ButtonGrid(origin=(0, 0), delta=(0, 0), button_shape=(96, 96), grid_shape=(1, 1))
    image = np.full((96, 96, 3), 180, np.uint8)
    ordinary = ItemGrid(grids, {})
    ordinary.amount_ocr = Mock(ocr_batch_with_validation=Mock(return_value=[2]))
    assert ordinary.predict(image)[0].name.isdigit()
    cloud = ItemGrid(grids, {})
    cloud.amount_ocr = ordinary.amount_ocr
    cloud.cloud_resolver = Mock(return_value={'item': 'Blueprint_2', 'source_template_id': 'U-ORIGINAL', 'tag': ''})
    item = cloud.predict(image)[0]
    assert item.name == 'Blueprint_2'
    assert item.source_template_id == 'U-ORIGINAL'
    assert item.amount == 2


def test_commission_frame_limit_does_not_publish_an_incomplete_record():
    from module.statistics.azurstats import AzurStats
    config = SimpleNamespace(config_name='test', DropRecord_CloudUpload=True)
    drop = AzurStats(config).new('commission', method='upload')
    with patch('module.statistics.commission_drop.classify_frame', side_effect=['done', 'reward'] * 41):
        for index in range(82):
            drop.add(np.zeros((720, 1280, 3), np.uint8))
    assert len(drop.images) == 80
    assert not drop.cloud_capture_valid
    with patch('module.statistics.commission_drop.record_commission_drops') as remote:
        drop.__exit__(None, None, None)
    remote.assert_not_called()


def test_different_consecutive_reward_pages_reach_pairing_validation():
    from module.statistics.azurstats import AzurStats
    config = SimpleNamespace(config_name='test', DropRecord_CloudUpload=True)
    drop = AzurStats(config).new('commission', method='upload')
    first = np.zeros((720, 1280, 3), np.uint8)
    second = np.ones_like(first)
    with patch('module.statistics.commission_drop.classify_frame', side_effect=['done', 'reward', 'reward', 'reward']):
        drop.add(first)
        drop.add(first)
        drop.add(first)  # An exact repeated reward capture is ignored.
        drop.add(second)  # A distinct page must be validated rather than silently discarded.
    assert len(drop.images) == 3
    assert np.array_equal(drop.images[-1], second)


def test_collectors_keep_observed_tags_separate_from_current_cloud_overrides():
    from module.cloud.catalog import map_items
    from module.statistics.commission_drop import CommissionReader
    from module.statistics.research_drop import ResearchDropParser
    item = SimpleNamespace(name='Blueprint_2', source_template_id='U-ORIGINAL',
                           tag='catchup', cloud_tag='bonus', amount=2, is_known_item=lambda: True)
    reader = object.__new__(CommissionReader)
    reader.stats = Mock()
    reader.stats.stats_get_items.return_value = [item]
    commission = reader.items(None)
    parser = object.__new__(ResearchDropParser)
    parser.stats = reader.stats
    with patch('module.research.assets.QUEUE_CHECK.match', return_value=False), \
            patch('module.combat.assets.GET_ITEMS_3.match', return_value=False):
        research = parser.parse([np.zeros((720, 1280, 3), np.uint8)]).source_items
    for source in [commission, research]:
        override = [{'unknown_id': 'U-ORIGINAL', 'item_id': 'Blueprint_2', 'tag': 'bonus'}]
        assert map_items(source, override)[0]['tag'] == 'bonus'
        override[0]['tag'] = ''
        assert map_items(source, override)[0]['tag'] == 'catchup'
