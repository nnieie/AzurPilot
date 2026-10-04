"""大世界月末清理的行动力保留、恢复异常和周期状态回归测试。"""

import unittest
from datetime import datetime
from unittest.mock import Mock, call, patch

from module.config.config import TaskEnd
from module.config.deep import deep_get, deep_set
from module.exception import GameStuckError, GameTooManyClickError, RequestHumanTakeover
from module.os.tasks.scheduling import OpsiScheduling


class MonthEndCleanupConfig:
    """仅保存智能调度状态，不读取真实账号配置或连接模拟器。"""

    def __init__(self, state=None):
        self.data = {'OpsiScheduling': {'Storage': {'Storage': dict(state or {})}}}
        self.modified = {}
        self.save_count = 0

    def cross_get(self, keys, default=None):
        return deep_get(self.data, keys=keys, default=default)

    def save(self):
        for keys, value in self.modified.items():
            deep_set(self.data, keys=keys, value=value)
        self.modified.clear()
        self.save_count += 1

    @staticmethod
    def task_stop():
        raise TaskEnd


class TestMonthEndCleanupActionPoint(unittest.TestCase):
    """每个子任务遵守月末保留值，达到保留值后停止后续行动力消耗。"""

    PRESERVE = 200

    def make_scheduling(self, action_points, first_run=True):
        scheduling = OpsiScheduling.__new__(OpsiScheduling)
        scheduling.config = MonthEndCleanupConfig({
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_FIRST_RUN: first_run,
        })
        scheduling._get_scheduling_action_point = Mock(side_effect=action_points)
        scheduling._run_scheduled_coin_task_once = Mock(return_value=True)
        scheduling._run_month_end_shop_purchase = Mock()
        scheduling._delay_smart_scheduling_to_server_update = Mock()
        scheduling.notify_push = Mock()
        return scheduling

    def run_cleanup(self, scheduling):
        with (
            patch('module.os.tasks.scheduling.get_os_reset_remain', return_value=1),
            self.assertRaises(TaskEnd),
        ):
            scheduling._run_month_end_cleanup(self.PRESERVE, 1000, 1000, 100)

    def test_passes_preserve_to_every_coin_task(self):
        scheduling = self.make_scheduling([
            (900, 100), (800, 100), (700, 100), (200, 100), (200, 100),
        ])

        self.run_cleanup(scheduling)

        self.assertEqual(scheduling._run_scheduled_coin_task_once.call_args_list, [
            call('OpsiStronghold', self.PRESERVE),
            call('OpsiObscure', self.PRESERVE),
            call('OpsiAbyssal', self.PRESERVE),
            call('OpsiMeowfficerFarming', self.PRESERVE),
        ])
        scheduling._run_month_end_shop_purchase.assert_called_once()

    def test_stops_after_stronghold_reaches_preserve(self):
        scheduling = self.make_scheduling([(200, 100), (200, 100)])

        self.run_cleanup(scheduling)

        scheduling._run_scheduled_coin_task_once.assert_called_once_with('OpsiStronghold', self.PRESERVE)
        scheduling._run_month_end_shop_purchase.assert_not_called()

    def test_stops_after_obscure_reaches_preserve(self):
        scheduling = self.make_scheduling([(250, 100), (200, 100), (200, 100)], first_run=False)

        self.run_cleanup(scheduling)

        scheduling._run_scheduled_coin_task_once.assert_called_once_with('OpsiObscure', self.PRESERVE)
        scheduling._run_month_end_shop_purchase.assert_not_called()

    def test_stops_after_abyssal_reaches_preserve(self):
        scheduling = self.make_scheduling([
            (350, 100), (250, 100), (200, 100), (200, 100),
        ], first_run=False)

        self.run_cleanup(scheduling)

        self.assertEqual(scheduling._run_scheduled_coin_task_once.call_args_list, [
            call('OpsiObscure', self.PRESERVE),
            call('OpsiAbyssal', self.PRESERVE),
        ])
        scheduling._run_month_end_shop_purchase.assert_not_called()

    def test_shop_recovery_exceptions_propagate_without_completion_notification(self):
        for exception_type in (TaskEnd, GameStuckError, GameTooManyClickError, RequestHumanTakeover):
            with self.subTest(exception=exception_type.__name__):
                scheduling = self.make_scheduling([
                    (900, 100), (800, 100), (700, 100), (200, 100), (200, 100),
                ], first_run=False)
                error = exception_type('月末商店流程需要退出或恢复')
                scheduling._run_month_end_shop_purchase.side_effect = error

                with self.assertRaises(exception_type) as raised:
                    scheduling._run_month_end_cleanup(self.PRESERVE, 1000, 1000, 100)

                self.assertIs(raised.exception, error)
                scheduling.notify_push.assert_not_called()
                scheduling._delay_smart_scheduling_to_server_update.assert_not_called()


class TestMonthEndCleanupCycle(unittest.TestCase):
    """首次清理状态按大世界重置周期持久化，与清理开关和运行间隔无关。"""

    def make_scheduling(self, state=None):
        scheduling = OpsiScheduling.__new__(OpsiScheduling)
        scheduling.config = MonthEndCleanupConfig(state)
        return scheduling

    def sync_cycle(self, scheduling, reset):
        with patch('module.os.tasks.scheduling.get_os_next_reset', return_value=reset):
            scheduling._reset_month_end_cleanup_first_run_if_new_month()

    def test_legacy_completed_flag_is_preserved_when_adding_cycle(self):
        scheduling = self.make_scheduling({
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_FIRST_RUN: False,
        })
        reset = datetime(2026, 11, 1)

        self.sync_cycle(scheduling, reset)

        self.assertFalse(scheduling._is_month_end_cleanup_first_run())
        self.assertEqual(
            scheduling._get_smart_scheduling_state_value(OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_CYCLE),
            reset.isoformat(),
        )

    def test_first_run_defaults_to_true_when_state_is_empty(self):
        scheduling = self.make_scheduling()

        self.sync_cycle(scheduling, datetime(2026, 11, 1))

        self.assertTrue(scheduling._is_month_end_cleanup_first_run())

    def test_next_month_resets_flag_without_an_intermediate_run(self):
        scheduling = self.make_scheduling({
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_FIRST_RUN: False,
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_CYCLE: datetime(2026, 11, 1).isoformat(),
        })

        # 两次运行都处于月末窗口，不能依靠清理模式曾退出判断是否跨月。
        with patch.object(scheduling, '_is_month_end_cleanup_active', return_value=True):
            self.sync_cycle(scheduling, datetime(2026, 12, 1))

        self.assertTrue(scheduling._is_month_end_cleanup_first_run())

    def test_same_month_switch_changes_keep_completed_flag(self):
        reset = datetime(2026, 11, 1)
        scheduling = self.make_scheduling({
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_FIRST_RUN: False,
            OpsiScheduling.STATE_KEY_MONTH_END_CLEANUP_CYCLE: reset.isoformat(),
        })

        for enabled in (False, True):
            with patch.object(scheduling, '_is_month_end_cleanup_active', return_value=enabled):
                self.sync_cycle(scheduling, reset)
            self.assertFalse(scheduling._is_month_end_cleanup_first_run())

        self.assertEqual(scheduling.config.save_count, 0)


class TestMonthEndCleanupIdleScheduling(unittest.TestCase):
    """月末清理达到保留线后始终延后，不能回到普通侵蚀 1 消耗剩余行动力。"""

    def make_scheduling(self, total_ap):
        scheduling = OpsiScheduling.__new__(OpsiScheduling)
        scheduling.config = MonthEndCleanupConfig()
        scheduling.is_running_prevent_action_point_overflow_task = Mock(return_value=False)
        scheduling.get_yellow_coins = Mock(return_value=60000)
        scheduling._get_scheduling_action_point = Mock(return_value=(total_ap, 100))
        scheduling._reset_month_end_cleanup_first_run_if_new_month = Mock()
        scheduling._is_month_end_cleanup_active = Mock(return_value=True)
        scheduling._get_month_end_action_point_preserve = Mock(return_value=1000)
        scheduling._run_month_end_cleanup = Mock()
        scheduling._get_smart_scheduling_operation_coins_preserve = Mock(return_value=40000)
        scheduling._get_effective_cl1_ap_preserve = Mock(return_value=200)
        scheduling._get_coin_task_action_point_preserve = Mock(return_value=200)
        scheduling._is_coin_target_scheduling_enabled = Mock(return_value=True)
        scheduling._execute_hazard1_leveling = Mock()
        scheduling._delay_smart_scheduling_with_minutes = Mock()
        scheduling._delay_smart_scheduling_to_server_update = Mock()
        return scheduling

    def run_idle_scheduling(self, scheduling, remain):
        with (
            patch('module.os.tasks.scheduling.get_os_reset_remain', return_value=remain),
            self.assertRaises(TaskEnd),
        ):
            scheduling.run_smart_scheduling_once()
        scheduling._run_month_end_cleanup.assert_not_called()
        scheduling._execute_hazard1_leveling.assert_not_called()

    def test_penultimate_day_waits_for_server_update_at_or_below_preserve(self):
        for total_ap in (500, 1000):
            with self.subTest(total_ap=total_ap):
                scheduling = self.make_scheduling(total_ap)

                self.run_idle_scheduling(scheduling, remain=1)

                scheduling._delay_smart_scheduling_to_server_update.assert_called_once_with('月末清理行动力不足')
                scheduling._delay_smart_scheduling_with_minutes.assert_not_called()

    def test_last_day_waits_two_hours_at_or_below_preserve(self):
        for total_ap in (500, 1000):
            with self.subTest(total_ap=total_ap):
                scheduling = self.make_scheduling(total_ap)

                self.run_idle_scheduling(scheduling, remain=0)

                scheduling._delay_smart_scheduling_with_minutes.assert_called_once_with(
                    '月末清理行动力不足（月底最后一天）', 120,
                )
                scheduling._delay_smart_scheduling_to_server_update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
