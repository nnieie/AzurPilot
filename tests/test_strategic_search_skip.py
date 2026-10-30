"""计划作战启动和确认弹窗的离线回归，不连接游戏。"""

import unittest
from unittest.mock import Mock

from module.os_handler.strategic import StrategicSearchHandler


class TestStrategicSearchSkipCheck(unittest.TestCase):
    """用 Mock 替换面板交互，验证侵蚀一配置对启动流程的分派。"""

    def make_handler(self, skip):
        handler = StrategicSearchHandler.__new__(StrategicSearchHandler)
        handler.config = Mock()
        handler.config.cross_get.return_value = skip
        handler.strategy_search_enter = Mock()
        handler.strategic_search_set_tab = Mock()
        handler.strategic_search_set_option = Mock(return_value=True)
        handler.strategic_search_confirm = Mock(return_value=True)
        return handler

    def test_default_still_checks_options(self):
        handler = self.make_handler(skip=False)
        self.assertTrue(handler.strategic_search_start(skip_first_screenshot=True))
        handler.strategy_search_enter.assert_called_once_with()
        handler.strategic_search_set_tab.assert_called_once_with()
        handler.strategic_search_set_option.assert_called_once_with()
        handler.strategic_search_confirm.assert_called_once_with()

    def test_skip_check_confirms_directly(self):
        handler = self.make_handler(skip=True)
        self.assertTrue(handler.strategic_search_start(skip_first_screenshot=True))
        handler.strategy_search_enter.assert_called_once_with()
        handler.strategic_search_set_tab.assert_called_once_with()
        handler.strategic_search_set_option.assert_not_called()
        handler.strategic_search_confirm.assert_called_once_with()

    def test_failed_option_check_retries(self):
        handler = self.make_handler(skip=False)
        handler.strategic_search_set_option = Mock(side_effect=[False, True])
        self.assertTrue(handler.strategic_search_start(skip_first_screenshot=True))
        self.assertEqual(handler.strategic_search_set_option.call_count, 2)
        handler.strategic_search_confirm.assert_called_once_with()

    def test_confirmation_failure_is_not_reported_as_success(self):
        handler = self.make_handler(skip=True)
        handler.strategic_search_confirm = Mock(return_value=False)
        self.assertFalse(handler.strategic_search_start())
        self.assertEqual(handler.strategic_search_confirm.call_count, 3)

    def test_gives_up_after_three_failures(self):
        handler = self.make_handler(skip=False)
        handler.strategic_search_set_option = Mock(return_value=False)
        self.assertFalse(handler.strategic_search_start(skip_first_screenshot=True))
        self.assertEqual(handler.strategic_search_set_option.call_count, 3)
        handler.strategic_search_confirm.assert_not_called()


class TestStrategicSearchConfirm(unittest.TestCase):
    """确认计划作战后，继续处理可能出现的指挥喵召回弹窗。"""

    def test_never_confirms_unidentified_secondary_popup(self):
        handler = StrategicSearchHandler.__new__(StrategicSearchHandler)
        handler.loop = Mock(return_value=range(2))
        handler.appear = Mock(side_effect=[True, False, True])
        handler.handle_popup_confirm = Mock(return_value=True)
        handler.is_in_map = Mock(return_value=False)

        self.assertFalse(handler.strategic_search_confirm())
        handler.handle_popup_confirm.assert_called_once_with(
            offset=(30, 30), name='STRATEGIC_SEARCH')
        handler.is_in_map.assert_not_called()

    def test_does_not_confirm_unknown_popup_before_search_panel(self):
        handler = StrategicSearchHandler.__new__(StrategicSearchHandler)
        handler.loop = Mock(return_value=range(1))
        handler.appear = Mock(return_value=False)
        handler.handle_popup_confirm = Mock()
        handler.is_in_map = Mock(return_value=True)

        self.assertTrue(handler.strategic_search_confirm())
        handler.handle_popup_confirm.assert_not_called()

    def test_returns_to_map_without_second_popup(self):
        handler = StrategicSearchHandler.__new__(StrategicSearchHandler)
        handler.loop = Mock(return_value=range(2))
        handler.appear = Mock(side_effect=[True, False, False])
        handler.handle_popup_confirm = Mock(return_value=True)
        handler.is_in_map = Mock(return_value=True)

        self.assertTrue(handler.strategic_search_confirm())
        handler.handle_popup_confirm.assert_called_once_with(
            offset=(30, 30), name='STRATEGIC_SEARCH')
        handler.is_in_map.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
