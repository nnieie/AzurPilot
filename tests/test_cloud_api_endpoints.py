"""Cloud URL migration keeps the client request and response contracts."""
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from deploy.git import GitManager
from module.base.api_client import ApiClient
from module.handover.handover import HANDOVER_MAINTAIN_API, OperationHandover
from module.api.announcement_service import AnnouncementService
from module.server_checker import SERVER_API_BASE


class CloudApiEndpointsTest(unittest.TestCase):
    def test_cl1_sends_raw_counters_with_v2(self):
        payload = {'device_id': 'a' * 32, 'instance_id': 'b' * 16, 'request_id': 'test-request',
                   'month': '2026-10', 'battle_count': 2, 'net_stamina_gain': 0}
        response = Mock(status_code=200, text=json.dumps({'schema_version': 2, 'request_id': 'test-request',
                                                         'accepted': True, 'duplicate': False}))
        with patch('module.base.api_client.http.post', return_value=response) as request:
            self.assertTrue(ApiClient._submit_cl1_data(payload, 5))
        self.assertEqual(request.call_args.args[0], 'https://api.azurpilot.com/api/v2/post/cl1')
        body = request.call_args.kwargs['json']
        self.assertEqual(body['akashi_ap'], 0)
        self.assertNotIn('net_stamina_gain', body)
        self.assertIn('request_id', body)
        self.assertIn('collected_at', body)
        self.assertEqual(request.call_args.kwargs['headers']['X-AzurPilot-ID'], 'a' * 32)

    def test_cl1_needs_a_matching_business_acknowledgement(self):
        payload = {'device_id': 'a' * 32, 'instance_id': 'b' * 16, 'request_id': 'expected',
                   'month': '2026-10', 'battle_count': 2}
        for reply in [{'status': 'success'}, {'schema_version': 2, 'request_id': 'wrong', 'accepted': True}]:
            with patch.object(ApiClient, '_post_with_fallback', return_value=(True, 200, json.dumps(reply))):
                self.assertFalse(ApiClient._submit_cl1_data(payload, 5))

    def test_identical_fallback_origin_does_not_send_twice(self):
        response = Mock(status_code=400)
        with patch('module.base.api_client.http.post', return_value=response) as request:
            success, status, _ = ApiClient._post_with_fallback('/bad', {})
        self.assertFalse(success)
        self.assertEqual(status, 400)
        request.assert_called_once()

    def test_announcement_keeps_root_id_contract(self):
        notice = {'id': 'notice-test', 'title': 'test', 'content': 'body', 'url': ''}
        response = Mock(status_code=200, text=json.dumps({'schema_version': 2, 'announcement': notice}))
        with patch('module.base.api_client.http.get', return_value=response) as request:
            service = AnnouncementService()
            self.assertEqual(service.get(force=True)['announcementId'], 'notice-test')
            self.assertEqual(request.call_args.args[0], 'https://api.azurpilot.com/api/v2/get/notice/data.json')
            response.text = json.dumps({'schema_version': 2, 'announcement': None})
            self.assertIsNone(service.get(force=True))

    def test_force_update_reads_boolean_from_new_origin(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                response = Mock(text=json.dumps(enabled))
                response.json.return_value = {'schema_version': 2, 'force_update': enabled}
                with patch('deploy.git.requests.get', return_value=response) as request:
                    self.assertIs(GitManager.cloud_force_update_enabled(), enabled)
                self.assertEqual(request.call_args.args[0], 'https://api.azurpilot.com/api/v2/get/update/data.json')

    def test_other_core_calls_use_same_origin(self):
        self.assertEqual(SERVER_API_BASE, 'https://api.azurpilot.com')
        self.assertEqual(HANDOVER_MAINTAIN_API, 'https://api.azurpilot.com/api/v2/get/maintenance')

    def test_maintenance_never_falls_back_from_jp_to_cn(self):
        target = SimpleNamespace(config=SimpleNamespace(SERVER='jp'))
        response = Mock()
        response.json.return_value = {'schema_version': 2, 'server': 'jp', 'maintenance': None}
        with patch('module.handover.handover.get_device_id', return_value='a' * 32), \
                patch('module.handover.handover.http.get', return_value=response) as request:
            result, _, error = OperationHandover.handover_maintain_query(target)
            self.assertEqual(result, {})
            self.assertEqual(error, '')
            self.assertEqual(request.call_args.kwargs['params'], {'server': 'jp'})
            response.json.return_value = {'schema_version': 2, 'server': 'cn', 'maintenance': {'starts_at': '2026-10-22T02:00:00Z'}}
            result, _, error = OperationHandover.handover_maintain_query(target)
            self.assertIsNone(result)
            self.assertTrue(error)

    def test_unavailable_maintenance_is_an_explicit_query_error(self):
        import requests
        target = SimpleNamespace(config=SimpleNamespace(SERVER='cn'))
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError('503 Service Unavailable')
        with patch('module.handover.handover.http.get', return_value=response):
            result, _, error = OperationHandover.handover_maintain_query(target)
        self.assertIsNone(result)
        self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()
