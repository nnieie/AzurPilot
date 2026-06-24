import copy
import os
import subprocess
import sys
from typing import Optional, Union

from deploy.logger import logger
from deploy.utils import *


class ExecutionError(Exception):
    pass


class ConfigModel:
    # Git
    Repository: str = "https://github.com/nnieie/AzurPilot"
    Branch: str = "master"
    GitExecutable: str = "./.venv/Scripts/git/cmd/git.exe" if sys.platform == "win32" else "./.venv/bin/git"
    GitProxy: Optional[str] = None
    SSLVerify: bool = False
    AutoUpdate: bool = True
    KeepLocalChanges: bool = False

    # Python 配置
    PythonExecutable: str = "./.venv/Scripts/python.exe" if sys.platform == "win32" else "./.venv/bin/python"
    PypiMirror: Optional[str] = None
    InstallDependencies: bool = True

    # ADB 配置
    AdbExecutable: str = "./.venv/Scripts/adb.exe" if sys.platform == "win32" else "./.venv/bin/adb"
    ReplaceAdb: bool = True
    AutoConnect: bool = True
    InstallUiautomator2: bool = True

    # OCR 配置
    UseOcrServer: bool = False
    StartOcrServer: bool = False
    OcrServerPort: int = 22268
    OcrClientAddress: str = "127.0.0.1:22268"

    # 更新配置
    EnableReload: bool = True
    CheckUpdateInterval: int = 5
    AutoRestartTime: str = "03:50"

    # 杂项
    DiscordRichPresence: bool = False

    # Remote Access
    EnableRemoteAccess: bool = True
    SSHUser: Optional[str] = None
    SSHServer: Optional[str] = "app.hk1.azurlane.cloud:10022"
    SSHExecutable: Optional[str] = None

    # WebUI 配置
    WebuiHost: str = "0.0.0.0"
    WebuiPort: int = 25548
    WebuiSSLKey: Optional[str] = None
    WebuiSSLCert: Optional[str] = None
    Language: str = "en-US"
    Theme: str = "default"
    DpiScaling: bool = True
    Password: Optional[str] = "123456"
    CDN: Union[str, bool] = False
    Run: Optional[str] = None

    # 动态配置
    GitOverCdn: bool = False


class DeployConfig(ConfigModel):
    def __init__(self, file=DEPLOY_CONFIG):
        """初始化部署配置。

        Args:
            file (str): 用户部署配置文件路径。
        """
        self.file = file
        self.template_file = get_deploy_template()
        self.config = {}
        self.config_template = {}
        self.read()

        self.show_config()

    def show_config(self):
        logger.hr("Show deploy config", 1)
        for k, v in self.config.items():
            if k in ("Password", "SSHUser"):
                continue
            if self.config_template.get(k) == v:
                continue
            logger.info(f"{k}: {v}")

        logger.info(f"Rest of the configs are the same as default")

    def read(self):
        """读取并更新部署配置，将配置值复制到属性。"""
        self.config = poor_yaml_read(self.template_file)
        self.config_template = copy.deepcopy(self.config)
        origin = poor_yaml_read(self.file)
        self.config.update(origin)

        for key, value in self.config.items():
            if hasattr(self, key):
                super().__setattr__(key, value)

        self.config_redirect()

        if self.config != origin:
            self.write()

    def write(self):
        poor_yaml_write(self.config, self.file, template_file=self.template_file)

    def config_redirect(self):
        """部署配置重定向，处理旧配置到新配置的迁移。

        每次 `read()` 之后必须调用。
        """
        if self.Repository in [
            'https://gitee.com/LmeSzinc/AzurLaneAutoScript',
            'https://gitee.com/lmeszinc/azur-lane-auto-script-mirror',
            'https://e.coding.net/llop18870/alas/AzurLaneAutoScript.git',
            'https://e.coding.net/saarcenter/alas/AzurLaneAutoScript.git',
            'https://git.saarcenter.com/LmeSzinc/AzurLaneAutoScript.git',
        ]:
            self.Repository = 'https://github.com/nnieie/AzurPilot'
            self.config['Repository'] = 'https://github.com/nnieie/AzurPilot'
        if self.PypiMirror in [
            'https://pypi.tuna.tsinghua.edu.cn/simple'
        ]:
            self.PypiMirror = 'https://mirrors.aliyun.com/pypi/simple'
            self.config['PypiMirror'] = 'https://mirrors.aliyun.com/pypi/simple'

        # 绕过 webui.config.DeployConfig.__setattr__()，不写入 deploy.yaml
        super().__setattr__(
            'GitOverCdn',
            False
        )
        if self.Repository in ['global']:
            super().__setattr__('Repository', 'https://github.com/nnieie/AzurPilot')
        if self.Repository in ['cn']:
            super().__setattr__('Repository', 'https://github.com/nnieie/AzurPilot')

    def filepath(self, key):
        """根据配置键获取绝对文件路径。

        Args:
            key (str): 配置键名。

        Returns:
            str: 绝对文件路径。
        """
        return (
            os.path.abspath(os.path.join(self.root_filepath, self.config[key]))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    @cached_property
    def root_filepath(self):
        return (
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    def execute(self, command, allow_failure=False, output=True):
        """执行系统命令。

        Args:
            command (str): 要执行的命令。
            allow_failure (bool): 是否允许失败。
            output (bool): 是否显示输出。

        Returns:
            bool: 是否成功。失败且不允许失败时终止安装流程。
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        if not output:
            command = command + ' >nul 2>nul'
        logger.info(command)
        # Using subprocess.call instead of os.system to better handle quoted paths with spaces on Windows
        error_code = subprocess.call(command, shell=True)
        if error_code:
            if allow_failure:
                logger.info(f"[ allowed failure ], error_code: {error_code}")
                return False
            else:
                logger.info(f"[ failure ], error_code: {error_code}")
                self.show_error(command)
                raise ExecutionError
        else:
            logger.info(f"[ success ]")
            return True

    def show_error(self, command=None):
        logger.hr("Update failed", 0)
        self.show_config()
        logger.info("")
        logger.info(f"Last command: {command}")
        logger.info(
            "Please check your deploy settings in config/deploy.yaml "
            "and re-open AzurPilot.exe"
        )
        logger.info("Take the screenshot of entire window if you need help")
