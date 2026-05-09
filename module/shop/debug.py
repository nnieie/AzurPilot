import json
import os
import re
import time
from datetime import datetime

from rich.console import Console
from rich.highlighter import NullHighlighter

from module.base.utils import save_image
from module.logger import RichFileHandler, file_formatter, logger


def _safe_name(name):
    name = str(name or 'unknown')
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .') or 'unknown'


class ShopDebugSession:
    """
    Per shop-task debug dump.

    This does not affect recognition or screenshots used by ALAS. It only keeps
    a USB/current screenshot, an ADB reference screenshot, and a copy of logs
    emitted during the shop task.
    """

    def __init__(self, main, label='shop'):
        config_name = _safe_name(getattr(main.config, 'config_name', 'unknown'))
        task = getattr(getattr(main.config, 'task', None), 'command', 'unknown')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        folder_name = f'{timestamp}_{_safe_name(task)}_{_safe_name(label)}'

        self.main = main
        self.folder = os.path.join('log', 'shop_debug', config_name, folder_name)
        self.log_file = os.path.join(self.folder, 'shop.log')
        self.handler = None
        self.stream = None
        self.capture_count = 0

    def start(self):
        if self.handler is not None:
            return

        os.makedirs(self.folder, exist_ok=True)
        self.stream = open(self.log_file, 'a', encoding='utf-8')
        console = Console(file=self.stream, no_color=True, highlight=False, width=119)
        self.handler = RichFileHandler(
            console=console,
            show_path=False,
            show_time=False,
            show_level=False,
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            tracebacks_extra_lines=3,
            highlighter=NullHighlighter(),
        )
        self.handler.setFormatter(file_formatter)
        logger.addHandler(self.handler)
        logger.info(f'Shop debug session: {self.folder}')

    def stop(self):
        if self.handler is None:
            return

        logger.info(f'Shop debug log saved: {self.log_file}')
        logger.removeHandler(self.handler)
        self.handler.close()
        self.handler = None
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def capture_entry(self, main=None, phase='entry'):
        main = main or self.main
        self.capture_count += 1
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        prefix = f'{self.capture_count:02d}_{_safe_name(phase)}_{timestamp}'
        method = getattr(main.config, 'Emulator_ScreenshotMethod', 'unknown')
        usb_name = 'usb.png' if method == 'usb_capture' else f'current_{_safe_name(method)}.png'
        usb_file = os.path.join(self.folder, f'{prefix}_{usb_name}')
        adb_file = os.path.join(self.folder, f'{prefix}_adb.png')
        meta_file = os.path.join(self.folder, f'{prefix}_meta.json')

        meta = {
            'time': datetime.now().isoformat(timespec='milliseconds'),
            'phase': phase,
            'config_name': getattr(main.config, 'config_name', 'unknown'),
            'task': getattr(getattr(main.config, 'task', None), 'command', 'unknown'),
            'screenshot_method': method,
            'control_method': getattr(main.config, 'Emulator_ControlMethod', 'unknown'),
            'serial': getattr(main.device, 'serial', 'unknown'),
            'package': getattr(main.device, 'package', 'unknown'),
            'usb_file': usb_file,
            'adb_file': adb_file,
            'log_file': self.log_file,
        }

        try:
            start = time.time()
            usb_image = main.device.screenshot()
            meta['usb_capture_ms'] = round((time.time() - start) * 1000, 3)
            save_image(usb_image, usb_file)
            logger.info(f'Shop debug USB screenshot saved: {usb_file}')
        except Exception as e:
            meta['usb_error'] = repr(e)
            logger.warning(f'Shop debug USB screenshot failed: {e}')

        try:
            start = time.time()
            adb_image = main.device.screenshot_adb()
            meta['adb_capture_ms'] = round((time.time() - start) * 1000, 3)
            save_image(adb_image, adb_file)
            logger.info(f'Shop debug ADB screenshot saved: {adb_file}')
        except Exception as e:
            meta['adb_error'] = repr(e)
            logger.warning(f'Shop debug ADB screenshot failed: {e}')

        try:
            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f'Shop debug metadata failed: {e}')
