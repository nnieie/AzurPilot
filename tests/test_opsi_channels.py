"""日志数值和既有遥测接口保持兼容；不输出堆栈局部变量。"""
import unittest
from unittest.mock import patch
from module.base.api_client import ApiClient
from module.logger import console_hdlr
from module.statistics.cl1_data_submitter import Cl1DataSubmitter

class ChannelTests(unittest.TestCase):
    def test_explicit_telemetry_remains_available(self):
        submitter = object.__new__(Cl1DataSubmitter)
        data = {'battle_count': 987}
        with patch.object(ApiClient, 'submit_cl1_data') as send:
            self.assertTrue(submitter.submit_data(data))
            send.assert_called_once_with(data, timeout=10)

    def test_exception_locals_are_not_logged(self):
        self.assertFalse(console_hdlr.tracebacks_show_locals)
