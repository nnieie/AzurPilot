# 此文件专门用于处理设备端的文本输入功能。
# 封装了检查输入法窗口状态以及向安卓组件发送文本指令的逻辑。
import base64
import time

from module.device.method.uiautomator_2 import Uiautomator2
from module.logger import logger


FAST_INPUT_IME = 'com.github.uiautomator/.FastInputIME'


class Input(Uiautomator2):
    def ime_shown(self) -> bool:
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
