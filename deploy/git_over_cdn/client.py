import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Generic, TypeVar

import requests
from requests.adapters import HTTPAdapter

T = TypeVar("T")

TEMPLATE_FILE = './config/template.yaml'

DEVICE_ID_HEADER = 'X-Device-Id'
CLOUDFLARE_VERSION_KEY_HEADER = 'Cloudflare-Workers-Version-Key'


def _read_device_id() -> str:
    """从 log/device_id.json 读取当前设备 ID，文件缺失或损坏时返回空字符串。

    Returns:
        str: 设备 ID 字符串。
    """
    try:
        path = Path(__file__).resolve().parents[2] / 'log' / 'device_id.json'
        device_id = json.loads(path.read_text(encoding='utf-8')).get('device_id', '')
        return device_id if isinstance(device_id, str) else ''
    except Exception:
        return ''


class cached_property(Generic[T]):
    """带类型支持的缓存属性描述符。

    属性只在首次访问时计算一次，之后替换为普通属性。
    删除属性后会重新计算。
    """

    def __init__(self, func: Callable[..., T]):
        """初始化缓存属性描述符。

        Args:
            func (Callable): 用于计算属性值的函数。
        """
        self.func = func

    def __get__(self, obj, cls) -> T:
        """获取属性值，未计算时调用底层函数并写入实例字典。

        Args:
            obj: 宿主对象实例。
            cls: 宿主类。

        Returns:
            T: 属性值。
        """
        if obj is None:
            return self

        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


class PrintLogger:
    """简易打印日志器，用于独立客户端的回退输出。"""
    info = print
    warning = print
    error = print

    @staticmethod
    def attr(name, text):
        """格式化输出属性名称与值。

        Args:
            name (str): 属性名称。
            text (str): 属性内容。
        """
        print(f'[{name}] {text}')


class GitOverCdnClient:
    """Git over CDN 客户端，通过 CDN 节点下载 git pack 包实现轻量快速更新。"""
    logger = PrintLogger()

    def __init__(self, url, folder, source='origin', branch='master', git='git', fallback_urls=None):
        """初始化 Git over CDN 客户端。

        Args:
            url (str | list[str]): 优先 CDN 服务地址，如 'http://127.0.0.1:22251/pack/...'。
            folder: 本地仓库路径，如 'D:/AzurLaneAutoScript'。
            source: 远程源名称，默认 'origin'。
            branch: 分支名称，默认 'master'。
            git: git 可执行文件路径。
            fallback_urls (str | list[str] | None): 所有优先地址不可用时使用的回退地址。
        """
        self.urls = self._normalize_urls(url)
        self.fallback_urls = self._normalize_urls(fallback_urls)
        all_urls = self.urls + self.fallback_urls
        if not all_urls:
            raise ValueError('至少需要一个 CDN 地址')
        self.url = all_urls[0]
        self.folder = folder.replace('\\', '/')
        self.source = source
        self.branch = branch
        self.git = git

    @staticmethod
    def _normalize_urls(urls):
        if urls is None:
            return []
        if isinstance(urls, str):
            return [urls.strip('/')]
        return [url.strip('/') for url in urls]

    def filepath(self, path):
        """获取本地 .git 目录下的绝对文件路径。

        Args:
            path (str): 相对 .git 目录的路径。

        Returns:
            str: 格式化为正斜杠的绝对路径。
        """
        path = os.path.join(self.folder, '.git', path)
        return os.path.abspath(path).replace('\\', '/')

    def urlpath(self, path, base=None):
        """拼接 CDN 请求的完整 URL。

        Args:
            path (str): 请求相对路径。
            base (str, optional): 基础 URL，默认使用当前选定的 CDN 地址。

        Returns:
            str: 拼接后的完整 URL。
        """
        if base is None:
            base = self.url
        return f'{base}{path}'

    @cached_property
    def current_commit(self) -> str:
        """获取本地仓库当前 HEAD 对应的完整 commit SHA。

        Returns:
            str: 40 位十六进制 commit 哈希，失败时返回空字符串。
        """
        # 以实际 HEAD 为准，兼容 packed-refs，避免旧的远端引用选错历史或更新包。
        try:
            result = subprocess.run(
                [self.git, 'rev-parse', '--verify', 'HEAD'], cwd=self.folder,
                capture_output=True, text=True, timeout=10,
            )
            commit = result.stdout.strip()
            if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', commit):
                self.logger.attr('CurrentCommit', commit)
                return commit
        except (OSError, subprocess.TimeoutExpired) as e:
            self.logger.error(f'Failed to get local commit: {e}')
        return ''

    @staticmethod
    def _create_session(max_retries=3):
        """创建配置了请求头和重试策略的 requests.Session。

        Args:
            max_retries (int): 最大重试次数。

        Returns:
            requests.Session: 已配置的会话对象。
        """
        session = requests.Session()
        session.trust_env = False
        device_id = _read_device_id()
        # X-Device-Id 供 CDN 统计设备；Cloudflare-Workers-Version-Key 供 Cloudflare
        # 灰度亲和路由（同一设备始终命中同一 Worker 版本）
        session.headers[DEVICE_ID_HEADER] = device_id
        session.headers[CLOUDFLARE_VERSION_KEY_HEADER] = device_id
        session.mount('http://', HTTPAdapter(max_retries=max_retries))
        session.mount('https://', HTTPAdapter(max_retries=max_retries))
        return session

    @cached_property
    def session(self):
        """获取复用的 HTTP 会话对象。

        Returns:
            requests.Session: 会话对象。
        """
        return self._create_session()

    def probe_url(self, url_base, timeout=3):
        """在给定总时限内探测候选地址的可用性与延迟。

        Args:
            url_base (str): 候选 CDN 基础 URL。
            timeout (float): 探测超时时间（秒），默认为 3。

        Returns:
            float | None: 响应耗时（秒），失败或超时返回 None。
        """
        url = self.urlpath('/latest.json', base=url_base)
        started = time.perf_counter()
        session = self._create_session(max_retries=0)
        try:
            with session.get(url, timeout=timeout, allow_redirects=False) as resp:
                elapsed = time.perf_counter() - started
                if elapsed > timeout:
                    self.logger.info(f'Probe GET {url} exceeded {timeout}s')
                    return None
                if resp.status_code != 200:
                    self.logger.info(f'Probe GET {url} failed, status={resp.status_code}')
                    return None
                try:
                    commit = json.loads(resp.text)['commit']
                except (json.JSONDecodeError, KeyError, TypeError):
                    self.logger.info(f'Probe GET {url} failed, invalid latest.json')
                    return None
                if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                    self.logger.info(f'Probe GET {url} failed, invalid commit')
                    return None
                self.logger.info(f'Probe GET {url} -> {elapsed:.3f}s')
                return elapsed
        except Exception as e:
            self.logger.info(f'Probe GET {url} failed: {e}')
        finally:
            session.close()
        return None

    def _probe_urls(self, urls, timeout):
        """并发测速并返回在时限内可用的地址，按延迟升序排序。

        Args:
            urls (list[str]): 待探测的 URL 列表。
            timeout (float): 探测时限（秒）。

        Returns:
            list[str]: 测速通过且按延迟排序的 URL 列表。
        """
        scored = []
        if not urls:
            return scored

        with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix='cdn-probe') as executor:
            futures = {
                executor.submit(self.probe_url, url_base, timeout): (index, url_base)
                for index, url_base in enumerate(urls)
            }
            for future in as_completed(futures):
                index, url_base = futures[future]
                try:
                    latency = future.result()
                except Exception as e:
                    self.logger.info(f'Probe {url_base} failed: {e}')
                    continue
                if latency is not None and latency <= timeout:
                    scored.append((latency, index, url_base))

        scored.sort(key=lambda item: (item[0], item[1]))
        return [url_base for _, _, url_base in scored]

    @cached_property
    def preferred_urls(self):
        """获取按测速延迟排序后的 CDN 地址列表，若探测超时则回退到备用列表。

        Returns:
            list[str]: 排序后的有效 CDN 节点列表。
        """
        ordered = self._probe_urls(self.urls, timeout=5)
        if ordered:
            self.logger.attr('PreferredUrl', ordered[0])
            return ordered

        if self.fallback_urls:
            self.logger.warning('No primary CDN url passed probe within 5s, using fallback urls')
            return self.fallback_urls

        self.logger.warning('No CDN url passed probe within 5s, fall back to configured order')
        return self.urls

    @cached_property
    def latest_commit(self) -> str:
        """从优先 CDN 节点拉取最新的 commit 哈希。

        Returns:
            str: 40 位 commit SHA，失败时返回空字符串。
        """
        for url_base in self.preferred_urls:
            self.url = url_base
            url = self.urlpath('/latest.json')
            self.logger.info(f'Fetch url: {url}')
            try:
                resp = self.session.get(url, timeout=3)
            except Exception as e:
                self.logger.error(f'Failed to get remote commit: {e}')
                continue

            if resp.status_code == 200:
                try:
                    info = json.loads(resp.text)
                    if not isinstance(info, dict) or info.get('branch', 'master') != self.branch:
                        self.logger.warning('CDN manifest does not match the configured branch')
                        continue
                    commit = info['commit']
                    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                        self.logger.error('CDN manifest contains an invalid commit')
                        continue
                    self.logger.attr('LatestCommit', commit)
                    return commit
                except json.JSONDecodeError:
                    self.logger.error(f'Failed to get remote commit, response is not a json: {resp.text}')
                except KeyError:
                    self.logger.error(f'Failed to get remote commit, key "commit" is not found: {resp.text}')
            else:
                self.logger.error(f'Failed to get remote commit, status={resp.status_code}, text={resp.text}')

        self.url = (self.urls + self.fallback_urls)[0]
        return ''

    def download_pack(self):
        """从 CDN 下载增量 pack 压缩包并解压至本地 objects/pack 目录。

        Returns:
            bool: 下载并解压是否成功。
        """
        latest = self.latest_commit
        current = self.current_commit
        for url_base in self.preferred_urls:
            self.url = url_base
            url = self.urlpath(f'/{latest}/{current}.zip')
            self.logger.info(f'Fetch url: {url}')
            try:
                resp = self.session.get(url, timeout=20)
            except Exception as e:
                self.logger.error(f'Failed to download pack: {e}')
                continue

            if resp.status_code != 200:
                self.logger.error(f'Failed to download pack, status={resp.status_code}, text={resp.text}')
                continue

            try:
                zipped = zipfile.ZipFile(io.BytesIO(resp.content))
                for file in [f'pack-{latest}.pack', f'pack-{latest}.idx']:
                    self.logger.info(f'Unzip {file}')
                    member = zipped.getinfo(file)
                    tmp = self.filepath(f'./objects/pack/{file}.tmp')
                    out = self.filepath(f'./objects/pack/{file}')
                    with zipped.open(member) as source, open(tmp, "wb") as target:
                        shutil.copyfileobj(source, target)
                    os.replace(tmp, out)
                return True
            except zipfile.BadZipFile as e:
                # 文件不是有效的 zip 文件
                self.logger.error(e)
            except KeyError as e:
                # 归档中不存在该文件
                self.logger.error(e)
            except Exception as e:
                self.logger.error(e)

        return False

    def update_refs(self):
        """将远端引用文件更新为最新 commit SHA。

        Returns:
            bool: 写入引用是否成功。
        """
        file = self.filepath(f'./refs/remotes/{self.source}/{self.branch}')
        text = f'{self.latest_commit}\n'
        self.logger.info(f'Update refs: {file}')
        os.makedirs(os.path.dirname(file), exist_ok=True)
        try:
            with open(file, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            return True
        except FileNotFoundError as e:
            self.logger.error(f'Failed to get local commit: {e}')
        except Exception as e:
            self.logger.error(f'Failed to get local commit: {e}')

        return False

    def git_command(self, *args, timeout=300):
        """在子进程中执行 git 命令。

        通常用于拉取或推送大文件。

        Args:
            timeout (int): 超时秒数，默认 300。

        Returns:
            str: 命令的标准输出。
        """
        cmd = list(map(str, args))
        cmd = [self.git] + cmd
        self.logger.info(f'Execute: {cmd}')

        process = subprocess.Popen(cmd, cwd=self.folder, stdout=subprocess.PIPE, shell=False)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            self.logger.warning(f'TimeoutExpired when calling {cmd}, stdout={stdout}, stderr={stderr}')
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd, output=stdout)
        return stdout.decode()

    def git_reset(self):
        """执行 git reset --hard 到远程分支，清理锁文件并重置工作区。

        Returns:
            bool: 重置是否成功。
        """
        # 移除 git 锁文件
        for lock_file in [
            './.git/index.lock',
            './.git/HEAD.lock',
            f'./.git/refs/heads/{self.branch}.lock',
        ]:
            lock_file = os.path.join(self.folder, lock_file)
            if os.path.exists(lock_file):
                self.logger.info(f'Lock file {lock_file} exists, removing')
                os.remove(lock_file)
        try:
            self.git_command('reset', '--hard', f'{self.source}/{self.branch}')
        except (OSError, subprocess.CalledProcessError) as e:
            self.logger.error(f'Failed to reset local repository: {e}')
            return False
        self.__dict__.pop('current_commit', None)
        return True

    def get_status(self):
        """获取仓库状态。

        Returns:
            str: 'uptodate' 表示已是最新，'behind' 表示落后于远程，'failed' 表示获取失败。
        """
        _ = self.current_commit
        _ = self.latest_commit
        if not self.current_commit:
            self.logger.error('Failed to get current commit')
            return 'failed'
        if not self.latest_commit:
            self.logger.error('Failed to get latest commit')
            return 'failed'
        if self.current_commit == self.latest_commit:
            self.logger.info('Already up to date')
            return 'uptodate'
        self.logger.info('Current repo is behind remote')
        return 'behind'

    def update(self):
        """通过 CDN 更新仓库。

        Returns:
            bool: 仓库是否已是最新。
        """
        _ = self.current_commit
        _ = self.latest_commit
        if not self.current_commit:
            self.logger.error('Failed to get current commit')
            return False
        if not self.latest_commit:
            self.logger.error('Failed to get latest commit')
            return False
        if self.current_commit == self.latest_commit:
            self.logger.info('Already up to date')
            # HEAD 可能已更新而远端引用仍旧，先对齐引用再 reset，避免回退版本。
            if not self.update_refs():
                return False
            return self.git_reset()

        if not self.download_pack():
            return False
        if not self.update_refs():
            return False
        if not self.git_reset():
            return False
        self.logger.info('Update success')
        return True
