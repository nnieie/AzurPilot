"""掉落配置只展示实际能力；迁移不修改用户文件、不连接云端。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import numpy as np

from module.config.config_generated import GeneratedConfig
from module.config.config_updater import ConfigUpdater
from module.config.redirect_utils.utils import OPSI_RECORD_ARGS
from module.statistics.azurstats import AzurStats
from module.statistics.cloud_upload import permitted, update_policy


def migrate(record):
    return ConfigUpdater().config_update({'Alas': {'DropRecord': record}})['Alas']['DropRecord']


@pytest.mark.parametrize('old, expected', [
    ('save', 'save_and_upload'), ('upload', 'upload'),
    ('save_and_upload', 'save_and_upload'), ('do_not', 'do_not'),
])
def test_cloud_modes_migrate_without_changing_explicit_disablement(old, expected):
    result = migrate({'ResearchRecord': old, 'CommissionRecord': old, 'CloudUpload': False})
    assert result['ResearchRecord'] == result['CommissionRecord'] == expected
    assert result['CloudUpload'] is False
    assert not permitted(SimpleNamespace(**{f'DropRecord_{k}': v for k, v in result.items()}), 'research')
    assert migrate(result) == result


@pytest.mark.parametrize('old, expected', [
    ('save', 'save'), ('save_and_upload', 'save'), ('upload', 'do_not'), ('do_not', 'do_not'),
])
def test_local_modes_keep_screenshot_intent_and_migrate_old_shared_switch(old, expected):
    result = migrate({'OpsiRecord': old, 'CombatRecord': old, 'MeowfficerTalent': old})
    for arg in (*OPSI_RECORD_ARGS, 'CombatRecord', 'MeowfficerTalent'):
        assert result[arg] == expected
    assert 'OpsiRecord' not in result
    assert migrate(result) == result


def test_defaults_and_removed_settings_match_generated_schema():
    updater = ConfigUpdater()
    schema = updater.args['Alas']['DropRecord']
    template = json.loads(Path('config/template.json').read_text(encoding='utf-8'))['Alas']['DropRecord']
    migrated = migrate({'API': 'cn_gz_reverse_proxy', 'AzurStatsID': 'obsolete'})
    assert 'API' not in migrated and 'AzurStatsID' not in migrated
    for arg in ('ResearchRecord', 'CommissionRecord'):
        assert schema[arg]['option'] == ['upload', 'save_and_upload', 'do_not']
        assert schema[arg]['value'] == template[arg] == migrated[arg] == 'upload'
        assert getattr(GeneratedConfig, f'DropRecord_{arg}') == 'upload'
    for arg in (*OPSI_RECORD_ARGS, 'CombatRecord', 'MeowfficerBuy', 'MeowfficerTalent'):
        assert schema[arg]['option'] == ['save', 'do_not']
        assert schema[arg]['value'] == template[arg] == 'do_not'
    for path in Path('module/config/i18n').glob('*.json'):
        words = json.loads(path.read_text(encoding='utf-8'))['DropRecord']
        assert 'API' not in words and 'AzurStatsID' not in words
        for arg in ('ResearchRecord', 'CommissionRecord'):
            assert 'save' not in words[arg]
        for arg in OPSI_RECORD_ARGS:
            assert 'upload' not in words[arg] and 'save_and_upload' not in words[arg]


@pytest.mark.parametrize('scene', ['research', 'commission'])
@pytest.mark.parametrize('method, save, upload', [
    ('upload', False, True), ('save_and_upload', True, True), ('do_not', False, False),
])
@pytest.mark.parametrize('enabled', [False, True])
def test_cloud_collection_matrix_keeps_local_statistics(scene, method, save, upload, enabled):
    config = SimpleNamespace(config_name='isolated', DropRecord_CloudUpload=enabled,
                             DropRecord_ResearchRecord=method, DropRecord_CommissionRecord=method)
    with patch('module.statistics.cloud_upload.resume_pending'), \
            patch('module.statistics.azurstats.cleanup_drop_screenshots_if_due'):
        drop = AzurStats(config).new(scene, method=method)
    assert drop.save is save
    assert drop.upload is (upload and enabled)
    assert drop.analyze is (scene == 'research')
    assert permitted(config, scene) is drop.upload


def test_rebound_queue_uses_new_scene_policy():
    queue = Mock(config_name='isolated')
    config = SimpleNamespace(config_name='isolated', DropRecord_CloudUpload=True,
                             DropRecord_ResearchRecord='do_not', DropRecord_CommissionRecord='upload')
    with patch('module.statistics.cloud_upload._queues', {'test': queue}):
        update_policy(config)
        assert queue.enabled()
        assert queue.scene_enabled('commission')
        assert not queue.scene_enabled('research')
        config.DropRecord_CommissionRecord = 'do_not'
        assert not queue.enabled()


def test_research_without_upload_still_commits_local_rewards():
    config = SimpleNamespace(config_name='isolated', DropRecord_CloudUpload=True,
                             DropRecord_ResearchRecord='do_not', DropRecord_CommissionRecord='do_not')
    with patch('module.statistics.cloud_upload.resume_pending'), \
            patch('module.statistics.azurstats.cleanup_drop_screenshots_if_due'), \
            patch('module.statistics.research_drop.record_research_drop') as parse:
        with AzurStats(config).new('research', method='do_not') as drop:
            drop.add(np.zeros((720, 1280, 3), np.uint8))
    parse.assert_called_once()
    assert parse.call_args.kwargs['instance'] == 'isolated'
    assert parse.call_args.kwargs['config'] is None
