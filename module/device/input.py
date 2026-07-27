"""设备文本输入模块。

封装 Android 设备端的文本输入功能，包括输入法窗口状态检测
以及通过 uiautomator2 向安卓组件发送文本指令和确认操作。
"""
# 此文件专门用于处理设备端的文本输入功能。
# 封装了检查输入法窗口状态以及向安卓组件发送文本指令的逻辑。
import base64
import time

from module.device.method.uiautomator_2 import Uiautomator2
from module.logger import logger


FAST_INPUT_IME = 'com.github.uiautomator/.FastInputIME'


class Input(Uiautomator2):
    """设备文本输入处理器。

    通过 uiautomator2 实现文本输入功能，包括输入法状态检测
    和带确认操作的文本输入。继承自 Uiautomator2 以获取底层输入接口。

    Methods:
        ime_shown: 检测输入法窗口是否显示。
        text_input_and_confirm: 输入文本并发送确认动作。
    """
    def ime_shown(self) -> bool:
        """检测当前输入法（IME）窗口是否正在显示。

        Returns:
            bool: 输入法窗口可见返回 True，否则返回 False。
        """
        _, shown = self.u2_current_ime()
        return shown

    def _wait_fastinput_ready(self, timeout: float = 3.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                ime_id, shown = self.u2_current_ime()
                if ime_id == FAST_INPUT_IME and shown:
                    return True
            except EnvironmentError:
                pass
            time.sleep(0.2)
        return False

    def _set_fastinput_ime_adb(self):
        self.adb_shell(['ime', 'enable', FAST_INPUT_IME], timeout=5)
        self.adb_shell(['ime', 'set', FAST_INPUT_IME], timeout=5)

    def _broadcast_fastinput_text(self, text: str, clear: bool = False):
        b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
        action = 'ADB_SET_TEXT' if clear else 'ADB_INPUT_TEXT'
        self.adb_shell(['am', 'broadcast', '-a', action, '--es', 'text', b64], timeout=5)

    def _broadcast_editor_action(self, code: int = 6):
        self.adb_shell(['am', 'broadcast', '-a', 'ADB_EDITOR_CODE', '--ei', 'code', code], timeout=5)

    def text_input_and_confirm(self, text: str, clear: bool=False):
        """向当前焦点输入框发送文本并按确认键（IME_ACTION_DONE）。

        失败时最多重试 3 次，适用于输入法偶尔无响应的场景。

        Args:
            text (str): 要输入的文本内容。
            clear (bool): 输入前是否清空输入框已有内容。
        """
        for fail_count in range(3):
            try:
                self._set_fastinput_ime_adb()
                if not self._wait_fastinput_ready():
                    logger.warning('FastInputIME is selected but not shown, try broadcast input anyway')
                self._broadcast_fastinput_text(text=text, clear=clear)
                time.sleep(0.2)
                self._broadcast_editor_action(6)
                break
            except EnvironmentError as e:
                logger.warning('FastInputIME broadcast input failed, fallback to uiautomator2')
                try:
                    self.u2_send_keys(text=text, clear=clear)
                    self.u2_send_action(6)
                    break
                except EnvironmentError as e:
                    pass
                if fail_count >= 2:
                    raise e
                logger.exception(str(e) + f'Retrying {fail_count + 1}/3')
                time.sleep(0.5)
