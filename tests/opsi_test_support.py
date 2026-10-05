"""统计用例只允许向临时安装目录注入运行服务。"""
import tempfile
from pathlib import Path
from module.statistics import opsi_secure
from tests.test_opsi_secure import MemoryProvider


def install_vault(case, folder):
    root = Path(folder).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError('测试目录未隔离')
    (root / 'config').mkdir(exist_ok=True)
    previous = opsi_secure._VAULT
    vault = opsi_secure.Vault(root, provider=MemoryProvider(), background_migration=False)
    opsi_secure.set_vault(vault)
    case.addCleanup(opsi_secure.set_vault, previous)
    return vault
