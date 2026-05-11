import configparser
import os
import shutil

from deploy.Windows.config import DeployConfig
from deploy.Windows.logger import Progress, logger
from deploy.Windows.utils import cached_property
from deploy.git_over_cdn.client import GitOverCdnClient


class GitConfigParser(configparser.ConfigParser):
    def check(self, section, option, value):
        result = self.get(section, option, fallback=None)
        if result == value:
            logger.info(f'Git config {section}.{option} = {value}')
            return True
        else:
            return False


class GitOverCdnClientWindows(GitOverCdnClient):
    def update(self, *args, **kwargs):
        Progress.GitInit()
        _ = super().update(*args, **kwargs)
        Progress.GitShowVersion()
        return _

    @cached_property
    def latest_commit(self) -> str:
        _ = super().latest_commit
        Progress.GitLatestCommit()
        return _

    def download_pack(self):
        _ = super().download_pack()
        Progress.GitDownloadPack()
        return _


class GitManager(DeployConfig):
    @staticmethod
    def remove(file):
        try:
            os.remove(file)
            logger.info(f'Removed file: {file}')
        except FileNotFoundError:
            logger.info(f'File not found: {file}')

    @cached_property
    def git_config(self):
        conf = GitConfigParser()
        conf.read('./.git/config')
        return conf

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

        if not self.execute(f'"{self.git}" status', allow_failure=True, output=False):
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
        self.execute(f'"{self.git}" init')
        # Check if remote exists before adding
        self.execute(f'"{self.git}" remote add "{source}" "{repo}"', allow_failure=True)
        # Set remote URL just in case it already exists
        self.execute(f'"{self.git}" remote set-url "{source}" "{repo}"')
        self.execute(f'"{self.git}" fetch "{source}" "{branch}"')
        self.execute(f'"{self.git}" reset --hard "{source}/{branch}"')

    def git_repository_init(
            self, repo, source='origin', branch='master',
            proxy='', ssl_verify=True, keep_changes=False
    ):
        if not self.git_repository_check():
            self.git_repository_repair(repo, source=source, branch=branch)

        logger.hr('Git Init', 1)
        if not self.execute(f'"{self.git}" init', allow_failure=True):
            self.remove('./.git/config')
            self.remove('./.git/index')
            self.remove('./.git/HEAD')
            self.remove('./.git/ORIG_HEAD')
            self.execute(f'"{self.git}" init')
        Progress.GitInit()

        logger.hr('Set Git Proxy', 1)
        if proxy:
            if not self.git_config.check('http', 'proxy', value=proxy):
                self.execute(f'"{self.git}" config --local http.proxy "{proxy}"')
            if not self.git_config.check('https', 'proxy', value=proxy):
                self.execute(f'"{self.git}" config --local https.proxy "{proxy}"')
        else:
            if not self.git_config.check('http', 'proxy', value=None):
                self.execute(f'"{self.git}" config --local --unset http.proxy', allow_failure=True)
            if not self.git_config.check('https', 'proxy', value=None):
                self.execute(f'"{self.git}" config --local --unset https.proxy', allow_failure=True)

        if ssl_verify:
            if not self.git_config.check('http', 'sslVerify', value='true'):
                self.execute(f'"{self.git}" config --local http.sslVerify true', allow_failure=True)
        else:
            if not self.git_config.check('http', 'sslVerify', value='false'):
                self.execute(f'"{self.git}" config --local http.sslVerify false', allow_failure=True)

        logger.hr('Set Git User-Agent', 1)
        self.execute(f'"{self.git}" config http.userAgent "ALAS/1.5.8 AzurPilot"')

        Progress.GitSetConfig()

        logger.hr('Set Git Repository', 1)
        if not self.git_config.check(f'remote "{source}"', 'url', value=repo):
            if not self.execute(f'"{self.git}" remote set-url "{source}" "{repo}"', allow_failure=True):
                self.execute(f'"{self.git}" remote add "{source}" "{repo}"')
        Progress.GitSetRepo()

        logger.hr('Fetch Repository Branch', 1)
        self.execute(f'"{self.git}" fetch "{source}" "{branch}"')
        Progress.GitFetch()

        logger.hr('Pull Repository Branch', 1)
        # Remove git lock
        for lock_file in [
            './.git/index.lock',
            './.git/HEAD.lock',
            './.git/refs/heads/master.lock',
        ]:
            if os.path.exists(lock_file):
                logger.info(f'Lock file {lock_file} exists, removing')
                os.remove(lock_file)
        if keep_changes:
            if self.execute(f'"{self.git}" stash', allow_failure=True):
                self.execute(f'"{self.git}" pull --ff-only "{source}" "{branch}"')
                if self.execute(f'"{self.git}" stash pop', allow_failure=True):
                    pass
                else:
                    # No local changes to existing files, untracked files not included
                    logger.info('Stash pop failed, there seems to be no local changes, skip instead')
            else:
                logger.info('Stash failed, this may be the first installation, drop changes instead')
                self.execute(f'"{self.git}" reset --hard "{source}/{branch}"')
                self.execute(f'"{self.git}" pull --ff-only "{source}" "{branch}"')
        else:
            self.execute(f'"{self.git}" reset --hard "{source}/{branch}"')
            Progress.GitReset()
            # Since `git fetch` is already called, checkout is faster
            if not self.execute(f'"{self.git}" checkout "{branch}"', allow_failure=True):
                self.execute(f'"{self.git}" pull --ff-only "{source}" "{branch}"')
            Progress.GitCheckout()

        logger.hr('Show Version', 1)
        self.execute(f'"{self.git}" --no-pager log --no-merges -1')
        Progress.GitShowVersion()

    @property
    def goc_client(self):
        # Resolve repo first to get the actual project name (e.g. AzurLaneAutoScript) instead of git.nanoda.work
        repo = self.resolve_repository_url(self.Repository)
        repo_name = repo.strip('/').split('/')[-1]
        url = f'https://vip.123pan.cn/1815343254/pack/LmeSzinc_{repo_name}_{self.Branch}'
        client = GitOverCdnClient(
            url=url,
            folder=self.root_filepath,
            source='origin',
            branch='master',
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
                import requests
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

    def git_install(self):
        logger.hr('Update Alas', 0)

        if not self.AutoUpdate:
            logger.info('AutoUpdate is disabled, skip')
            Progress.GitShowVersion()
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
