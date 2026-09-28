import shlex
import shutil

import requests

from deploy.config import DeployConfig, ExecutionError
from deploy.git_over_cdn.client import GitOverCdnClient
from deploy.git_over_cdn.endpoints import CLOUDFLARE_UPDATE_URLS, FALLBACK_UPDATE_URLS
from deploy.logger import logger
from deploy.utils import *


CLOUD_UPDATE_CONTROL_URL = 'https://alas-apiv2.nanoda.work/api/updata'
CLOUD_FORCE_UPDATE_CONTROL_URL = 'https://alas-apiv2.nanoda.work/api/force_update'


def _cmd(*args):
    return ' '.join(shlex.quote(str(arg)) for arg in args)


class GitManager(DeployConfig):
    """Git 仓库与更新管理类，负责源码拉取、分支同步及 GitOverCDN 更新。"""

    @cached_property
    def git(self):
        """获取 Git 可执行文件路径。

        Returns:
            str: Git 可执行文件绝对路径或回退命令 'git'。
        """
        exe = self.filepath('GitExecutable')
        if os.path.exists(exe):
            return exe

        logger.warning(f'GitExecutable: {exe} does not exist, use `git` instead')
        return 'git'

    @staticmethod
    def remove(file):
        """安全删除指定文件。

        Args:
            file (str): 待删除的文件路径。
        """
        try:
            os.remove(file)
            logger.info(f'Removed file: {file}')
        except FileNotFoundError:
            logger.info(f'File not found: {file}')

    def git_repository_check(self):
        """
        检查 .git 目录是否存在且未损坏。

        Returns:
            bool: True 表示仓库正常，False 表示缺失或损坏需要修复。
        """
        if not os.path.isdir('./.git'):
            logger.warning('.git directory does not exist')
            return False

        head_file = './.git/HEAD'
        if not os.path.exists(head_file):
            logger.warning('.git/HEAD does not exist, repository may be corrupted')
            return False

        try:
            with open(head_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if not content:
                logger.warning('.git/HEAD is empty, repository may be corrupted')
                return False
        except Exception as e:
            logger.warning(f'.git/HEAD is unreadable: {e}')
            return False

        if not self.execute(_cmd(self.git, 'status'), allow_failure=True, output=False):
            logger.warning('git status failed, repository may be corrupted')
            return False

        return True

    def git_repository_repair(self, repo, source='origin', branch='master'):
        """
        .git 缺失或损坏时，删除 .git 目录并重新 clone 仓库。
        """
        logger.hr('Git Repository Repair', 1)
        logger.warning('Attempting to repair git repository by re-cloning')

        if os.path.isdir('./.git'):
            logger.info('Removing corrupted .git directory')
            try:
                shutil.rmtree('./.git')
                logger.info('Removed .git directory')
            except Exception as e:
                logger.error(f'Failed to remove .git directory: {e}')
                raise

        logger.info(f'Initializing repository: {repo} branch: {branch}')
        self.execute(_cmd(self.git, 'init'))
        self.execute(_cmd(self.git, 'remote', 'add', source, repo), allow_failure=True)
        self.execute(_cmd(self.git, 'remote', 'set-url', source, repo))
        self.execute(_cmd(self.git, 'fetch', source, branch))
        self.execute(_cmd(self.git, 'reset', '--hard', f'{source}/{branch}'))

    def git_repository_init(
            self, repo, source='origin', branch='master',
            proxy='', ssl_verify=True, keep_changes=False
    ):
        if not self.git_repository_check():
            self.git_repository_repair(repo, source=source, branch=branch)

        logger.hr('Git Init', 1)
        if not self.execute(_cmd(self.git, 'init'), allow_failure=True):
            self.remove('./.git/config')
            self.remove('./.git/index')
            self.remove('./.git/HEAD')
            self.execute(_cmd(self.git, 'init'))

        logger.hr('Set Git Proxy', 1)
        if proxy:
            self.execute(_cmd(self.git, 'config', '--local', 'http.proxy', proxy))
            self.execute(_cmd(self.git, 'config', '--local', 'https.proxy', proxy))
        else:
            self.execute(_cmd(self.git, 'config', '--local', '--unset', 'http.proxy'), allow_failure=True)
            self.execute(_cmd(self.git, 'config', '--local', '--unset', 'https.proxy'), allow_failure=True)

        if ssl_verify:
            self.execute(_cmd(self.git, 'config', '--local', 'http.sslVerify', 'true'), allow_failure=True)
        else:
            self.execute(_cmd(self.git, 'config', '--local', 'http.sslVerify', 'false'), allow_failure=True)

        logger.hr('Set Git User-Agent', 1)
        self.execute(_cmd(self.git, 'config', 'http.userAgent', 'ALAS/1.5.8 AzurPilot'))

        logger.hr('Set Git Repository', 1)
        if not self.execute(_cmd(self.git, 'remote', 'set-url', source, repo), allow_failure=True):
            self.execute(_cmd(self.git, 'remote', 'add', source, repo))

        logger.hr('Fetch Repository Branch', 1)
        self.execute(_cmd(self.git, 'fetch', source, branch))

        logger.hr('Pull Repository Branch', 1)
        for lock_file in [
            './.git/index.lock',
            './.git/HEAD.lock',
            './.git/refs/heads/master.lock',
        ]:
            if os.path.exists(lock_file):
                logger.info(f'Lock file {lock_file} exists, removing')
                os.remove(lock_file)
        if keep_changes:
            if self.execute(_cmd(self.git, 'stash'), allow_failure=True):
                self.execute(_cmd(self.git, 'pull', '--ff-only', source, branch))
                if self.execute(_cmd(self.git, 'stash', 'pop'), allow_failure=True):
                    pass
                else:
                    logger.info('Stash pop failed, there seems to be no local changes, skip instead')
            else:
                logger.info('Stash failed, this may be the first installation, drop changes instead')
                self.execute(_cmd(self.git, 'reset', '--hard', f'{source}/{branch}'))
                self.execute(_cmd(self.git, 'pull', '--ff-only', source, branch))
        else:
            self.execute(_cmd(self.git, 'reset', '--hard', f'{source}/{branch}'))
            self.execute(_cmd(self.git, 'pull', '--ff-only', source, branch))

        logger.hr('Show Version', 1)
        self.execute(_cmd(self.git, '--no-pager', 'log', '--no-merges', '-1'))

    @property
    def goc_client(self):
        # Resolve repo first to get the actual project name
        repo = self.resolve_repository_url(self.Repository)
        repo_name = repo.strip('/').split('/')[-1]
        client = GitOverCdnClient(
            url=[
                f'https://vip.123pan.cn/1818706573/pack/LmeSzinc_{repo_name}_{self.Branch}',
                f'https://1818706573.v.123yx.com/1818706573/pack/LmeSzinc_{repo_name}_{self.Branch}',
            ],
            folder=self.root_filepath,
            source='origin',
            branch=self.Branch,
            git=self.git,
        )
        client.logger = logger
        return client

    def resolve_repository_url(self, url):
        """
        Resolve 307 redirects from git.nanoda.work to get the actual git repository URL.
        """
        if 'git.nanoda.work' in url:
            try:
                headers = {'User-Agent': 'alas AzurPilot'}
                logger.info(f'Resolving repository URL: {url}')
                # Follow all redirects to get the final destination
                response = requests.get(
                    url,
                    allow_redirects=True,
                    timeout=10,
                    headers=headers
                )
                if response.status_code == 200:
                    resolved = response.url.rstrip('/')
                    logger.info(f'Resolved {url} to {resolved}')
                    return resolved
                return url
            except Exception as e:
                logger.error(f'Failed to resolve {url}: {e}')
        return url

    @staticmethod
    def cloud_auto_update_enabled():
        """检查云端自动更新开关是否启用。

        Returns:
            bool | None: True 启用，False 禁用，网络异常返回 None。
        """
        logger.info(f'Check cloud update control: {CLOUD_UPDATE_CONTROL_URL}')
        try:
            resp = requests.get(CLOUD_UPDATE_CONTROL_URL, timeout=5, headers={'User-Agent': 'alas AzurPilot'})
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f'Failed to check cloud update control: {e}')
            return None

        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            data = text

        if data is True or (isinstance(data, str) and data.lower() in ('true', 'ture')):
            logger.info('Cloud update control is enabled')
            return True
        if data is False or (isinstance(data, str) and data.lower() in ('false', 'fales')):
            logger.info('Cloud update control is disabled')
            return False

        logger.info(f'Cloud update control is inaccessible: {text}')
        return None

    @staticmethod
    def cloud_force_update_enabled():
        """检查云端强制更新开关是否启用。

        Returns:
            bool | None: True 启用，False 禁用，网络异常返回 None。
        """
        logger.info(f'Check cloud force update control: {CLOUD_FORCE_UPDATE_CONTROL_URL}')
        try:
            resp = requests.get(
                CLOUD_FORCE_UPDATE_CONTROL_URL,
                timeout=5,
                headers={'User-Agent': 'alas AzurPilot'},
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f'Failed to check cloud force update control: {e}')
            return None

        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            data = text

        if data is True or (isinstance(data, str) and data.lower() in ('true', 'ture')):
            logger.info('Cloud force update control is enabled')
            return True
        if data is False or (isinstance(data, str) and data.lower() in ('false', 'fales')):
            logger.info('Cloud force update control is disabled')
            return False

        logger.info(f'Cloud update control is inaccessible: {text}')
        return None

    def cloud_update_access_failed(self, fatal=True):
        """处理云端更新控制接口访问失败的情形。

        Args:
            fatal (bool): 是否视为致命错误并终止启动。

        Raises:
            ExecutionError: 当 fatal 为 True 时抛出。
        """
        logger.hr('Cloud Update Control Failed', 0)
        if fatal:
            logger.warning('Failed to access cloud update control, stopping startup')
            raise ExecutionError
        else:
            logger.warning('Failed to access cloud update control, skip update check')

    def git_install(self):
        """根据云端状态与本地配置执行 Git 源码拉取与更新。"""
        logger.hr('Update AzurPilot', 0)

        cloud_update = self.cloud_auto_update_enabled()
        if cloud_update is None:
            self.cloud_update_access_failed()
        if not cloud_update:
            logger.info('Cloud update control disabled, skip')
            return

        if self.GitOverCdn:
            if self.goc_client.update(keep_changes=self.KeepLocalChanges):
                return

        # Resolve repository URL before any git operations
        repo = self.resolve_repository_url(self.Repository)

        self.git_repository_init(
            repo=repo,
            source='origin',
            branch=self.Branch,
            proxy=self.GitProxy,
            ssl_verify=self.SSLVerify,
            keep_changes=self.KeepLocalChanges,
        )


if __name__ == '__main__':
    self = GitManager()
    self.goc_client.get_status()
