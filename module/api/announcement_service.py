"""全局公告服务：负责从远端获取公告，支持缓存与并发保护。"""
import threading
import time
from typing import Any, Dict, Optional

from module.base.api_client import ApiClient
from module.logger import logger


class AnnouncementService:
    """提供公告缓存与获取能力。"""

    def __init__(self, ttl: int = 90):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: Optional[Dict[str, Any]] = None
        self._last_fetch: float = 0.0

    def get(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """
        获取当前最新公告。

        Args:
            force: 为 True 时跳过缓存 TTL 强制重新拉取

        Returns:
            公告数据字典，包含 announcementId, title, content, url 等；无公告时返回 None
        """
        now = time.time()
        with self._lock:
            if not force and self._cached is not None and (now - self._last_fetch < self._ttl):
                return self._cached

            cached_id = self._cached.get('announcementId') if self._cached else None
            try:
                # ApiClient.get_announcement 支持传入 current_id 进行增量检查
                data = ApiClient.get_announcement(timeout=5, current_id=cached_id)
                self._last_fetch = now
                if data is not None:
                    self._cached = data
                # 若返回 None 且之前有缓存，则保留已有缓存（如 304 Not Modified）
            except Exception as e:
                logger.warning(f'[API] 拉取公告失败: {e}')
                self._last_fetch = now

            return self._cached


announcement_service = AnnouncementService()
