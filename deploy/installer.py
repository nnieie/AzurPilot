from deploy.patch import pre_checks

pre_checks()

import sys

from deploy.adb import AdbManager
from deploy.alas import AlasManager
from deploy.app import AppManager
from deploy.config import ExecutionError
from deploy.git import GitManager
from deploy.pip import PipManager


class Installer(GitManager, PipManager, AdbManager, AppManager, AlasManager):
    """集成安装器，统筹 Git 更新、进程清理、依赖安装、前端构建及 ADB 服务。"""

    def install(self):
        """执行完整的安装与环境部署流程。

        失败时向 stderr 输出 ``AZURPILOT_ERROR:{code}:{message}`` 标记行
        并以分类错误码退出，供启动器结构化识别。
        """
        from deploy.atomic import atomic_failure_cleanup
        atomic_failure_cleanup('./config')
        try:
            self.git_install()
            self.alas_kill()
            self.pip_install()
            self.app_update()
            self.adb_install()
        except ExecutionError as e:
            code = getattr(e, 'code', 1)
            print(f'AZURPILOT_ERROR:{code}:{e}', file=sys.stderr, flush=True)
            exit(code)


if __name__ == '__main__':
    Installer().install()
