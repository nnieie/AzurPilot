"""公告服务与 API 分发回归测试。"""
import time
import unittest
from unittest.mock import Mock, patch

from module.api.announcement_service import AnnouncementService
from module.api.protocol import AnnouncementParams
from module.api.router import Router


class AnnouncementServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = AnnouncementService(ttl=2)

    def test_get_announcement_and_caching(self):
        mock_data = {
            'announcementId': 'test-id-1',
            'title': '测试公告',
            'content': '测试公告内容',
            'url': 'https://example.com',
        }
        with patch('module.base.api_client.ApiClient.get_announcement', return_value=mock_data) as mock_get:
            res1 = self.service.get(force=False)
            self.assertEqual(res1, mock_data)
            self.assertEqual(mock_get.call_count, 1)

            # TTL 内再次获取，直接命中缓存，不应再次调用远端
            res2 = self.service.get(force=False)
            self.assertEqual(res2, mock_data)
            self.assertEqual(mock_get.call_count, 1)

            # force=True 时应忽略缓存强制请求
            self.service.get(force=True)
            self.assertEqual(mock_get.call_count, 2)

    def test_304_preserves_existing_cache(self):
        initial_data = {
            'announcementId': 'id-100',
            'title': '旧公告',
            'content': '旧内容',
            'url': '',
        }
        with patch('module.base.api_client.ApiClient.get_announcement', return_value=initial_data):
            self.service.get(force=False)

        # 远端返回 None（例如 304 未修改）
        with patch('module.base.api_client.ApiClient.get_announcement', return_value=None):
            result = self.service.get(force=True)
            self.assertEqual(result, initial_data)

    def test_router_dispatch(self):
        router = Router(configs=None, runtime=None)
        mock_data = {
            'announcementId': 'router-test',
            'title': '路由测试',
            'content': '分发测试',
            'url': '',
        }
        with patch('module.base.api_client.ApiClient.get_announcement', return_value=mock_data):
            res = router.dispatch('announcement.get', {'force': True})
            self.assertEqual(res, mock_data)


if __name__ == '__main__':
    unittest.main()
