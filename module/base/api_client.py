"""API 客户端模块。

负责与 API 服务器进行 HTTP 交互，包括 CL1 数据提交和公告获取，
默认统一使用 api.azurpilot.com，保留既有的主备端点请求流程。
"""
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timezone
import uuid

import requests

from deploy.api_endpoints import API_BASE_URL
from module.logger import logger


class ApiClient:
    """统一的 API 客户端，保留主备端点配置。"""
    
    # 主域名和备用域名列表
    PRIMARY_DOMAIN = API_BASE_URL
    FALLBACK_DOMAIN = API_BASE_URL
    
    # API端点路径
    CL1_DATA_PATH = '/api/v2/post/cl1'
    ANNOUNCEMENT_PATH = '/api/v2/get/notice/data.json'

    # 公告检查间隔（秒），1.5分钟 = 90秒
    ANNOUNCEMENT_CHECK_INTERVAL = 90
    
    @classmethod
    def _get_endpoints(cls, path: str) -> List[str]:
        """获取指定路径的所有端点 URL（主域名 + 备用域名）。
        
        Args:
            path (str): API 相对路径。
            
        Returns:
            list[str]: 端点完整 URL 列表。
        """
        return list(dict.fromkeys([
            f'{cls.PRIMARY_DOMAIN}{path}',
            f'{cls.FALLBACK_DOMAIN}{path}'
        ]))
    
    @classmethod
    def _post_with_fallback(cls, path: str, json_data: Dict[str, Any], timeout: int = 5) -> Tuple[bool, int, str]:
        """使用故障转移机制发送 POST 请求。

        Args:
            path (str): API 相对路径。
            json_data (dict[str, Any]): JSON 载荷数据。
            timeout (int): 超时秒数。默认为 5 秒。

        Returns:
            tuple[bool, int, str]: (是否成功, HTTP 状态码, 响应文本或错误信息)。
        """
        return cls._request_with_fallback('POST', path, json_data=json_data, timeout=timeout)
    
    @classmethod
    def _get_with_fallback(cls, path: str, params: Dict[str, Any] = None, timeout: int = 10) -> Tuple[bool, int, str]:
        """使用故障转移机制发送 GET 请求。
        
        Args:
            path (str): API 相对路径。
            params (dict[str, Any] | None): URL 查询参数。
            timeout (int): 超时时间（秒）。
            
        Returns:
            tuple[bool, int, str]: (是否成功, HTTP 状态码, 响应文本)。
        """
        return cls._request_with_fallback('GET', path, params=params, timeout=timeout)

    @classmethod
    def _request_with_fallback(cls, method: str, path: str, params: Dict[str, Any] = None, 
                             json_data: Dict[str, Any] = None, timeout: int = 10,
                             success_codes: List[int] = None) -> Tuple[bool, int, str]:
        """通用请求方法，支持主备域名故障转移。

        Args:
            method (str): HTTP 方法，如 'GET' 或 'POST'。
            path (str): API 相对路径。
            params (dict[str, Any] | None): URL 查询参数。
            json_data (dict[str, Any] | None): POST JSON 载荷。
            timeout (int): 请求超时秒数。
            success_codes (list[int] | None): 视为成功的状态码列表，默认为 [200]。

        Returns:
            tuple[bool, int, str]: (是否成功, HTTP 状态码, 响应文本或错误信息)。
        """
        if success_codes is None:
            success_codes = [200]
            
        endpoints = cls._get_endpoints(path)
        last_error = None
        last_status = 0
        
        for i, endpoint in enumerate(endpoints):
            try:
                domain_type = "主域名" if i == 0 else "备用域名"
                logger.debug(f'[基础-API] 尝试使用{domain_type}: {endpoint}')
                
                if method == 'GET':
                    response = requests.get(
                        endpoint,
                        params=params,
                        timeout=timeout,
                        headers={'User-Agent': 'alas AzurPilot'}
                    )
                else:
                    response = requests.post(
                        endpoint,
                        json=json_data,
                        timeout=timeout,
                        headers={
                            'Content-Type': 'application/json',
                            'User-Agent': 'alas AzurPilot',
                            'X-AzurPilot-ID': str((json_data or {}).get('device_id', '')),
                        }
                    )
                
                if response.status_code in success_codes:
                    if i > 0:
                        logger.info(f'[基础-API] 使用{domain_type}请求成功')
                    return True, response.status_code, response.text
                else:
                    logger.warning(f'[基础-API] {domain_type}返回错误状态: {response.status_code}')
                    last_error = f'HTTP {response.status_code}'
                    last_status = response.status_code
                    if 400 <= last_status < 500 and last_status != 429:
                        break
                    
            except requests.exceptions.Timeout:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}请求超时')
                last_error = 'Timeout'
            except requests.exceptions.RequestException as e:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}请求失败: {e}')
                last_error = str(e)
            except Exception as e:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}发生异常: {e}')
                last_error = str(e)
        
        return False, last_status, last_error or 'Unknown error'
    
    @staticmethod
    def _submit_cl1_data(data: Dict[str, Any], timeout: int):
        """内部方法：提交 CL1 统计数据。
        
        Args:
            data (dict[str, Any]): 数据字典。
            timeout (int): 超时时间（秒）。
        """
        try:
            # 如果没有任何战斗数据,不提交
            if data.get('battle_count', 0) == 0:
                logger.info('无CL1战斗数据可提交')
                return
            
            logger.info(f'[基础-API] 提交CL1数据 {data.get("month", "unknown")}...')
            logger.attr('战斗次数', data.get('battle_count', 0))
            logger.attr('明石遭遇次数', data.get('akashi_encounters', 0))
            logger.attr('明石出现概率', f"{data.get('akashi_probability', 0):.2%}")
            
            payload = ApiClient.cl1_payload(data)
            success, status_code, response_text = ApiClient._post_with_fallback(
                ApiClient.CL1_DATA_PATH,
                payload,
                timeout=timeout
            )
            
            if success:
                import json
                acknowledgement = json.loads(response_text)
                if (not isinstance(acknowledgement, dict)
                        or acknowledgement.get('schema_version') != 2
                        or acknowledgement.get('request_id') != payload['request_id']
                        or (acknowledgement.get('accepted') is not True
                            and acknowledgement.get('duplicate') is not True)):
                    logger.warning('[基础-API] CL1 回执无效，等待下次快照提交')
                    return False
                logger.info('[基础-API] CL1 数据提交成功')
                return True
            else:
                logger.warning(f'[基础-API] CL1 数据提交失败: {response_text}')
                return False

        except Exception as e:
            logger.exception(f'[基础-API] CL1 数据提交异常: {e}')
            return False

    @staticmethod
    def cl1_payload(data: Dict[str, Any]) -> Dict[str, Any]:
        """Send raw monthly counters; the main statistics service computes derived metrics."""
        return {
            'request_id': data.get('request_id') or str(uuid.uuid4()),
            'device_id': data['device_id'],
            'instance_id': data['instance_id'],
            'month': data['month'],
            'collected_at': data.get('collected_at') or datetime.now(timezone.utc).isoformat(),
            'battle_count': data['battle_count'],
            'akashi_encounters': data.get('akashi_encounters', 0),
            'akashi_ap': data.get('akashi_ap', data.get('net_stamina_gain', 0)),
        }
    
    @classmethod
    def submit_cl1_data(cls, data: Dict[str, Any], timeout: int = 10):
        """提交 CL1 统计数据（异步执行）。

        仅上传经过哈希处理的设备 ID 与统计指标，不包含原始硬件敏感信息。

        Args:
            data (dict[str, Any]): 包含 device_id 和统计数据的字典。
            timeout (int): 请求超时时间（秒），默认 10 秒。
        """
        from module.base.async_executor import async_executor
        return async_executor.submit(cls._submit_cl1_data, data, timeout)

    @classmethod
    def get_announcement(cls, timeout: int = 1, current_id: int = None) -> Optional[Dict[str, Any]]:
        """获取公告信息（同步）。
        
        Args:
            timeout (int): 请求超时时间（秒），默认为 1 秒。
            current_id (int | None): 当前已知公告 ID，用于增量检查。
            
        Returns:
            dict[str, Any] | None: 公告数据字典；若无更新或获取失败则返回 None。
        """
        import time
        try:
            # 添加时间戳参数以绕过缓存
            timestamp = int(time.time())
            params = {'t': timestamp}
            if current_id is not None:
                params['id'] = current_id            
            # 允许 200 (OK) 和 304 (Not Modified)
            success, status_code, response_text = cls._request_with_fallback(
                'GET',
                cls.ANNOUNCEMENT_PATH,
                params=params,
                timeout=timeout,
                success_codes=[200, 304]
            )            
            if success:
                # 304 或空内容表示无更新
                if status_code == 304 or not response_text.strip():
                    return None
                    
                import json
                try:
                    data = json.loads(response_text)

                    if not isinstance(data, dict) or data.get('schema_version') != 2 or 'announcement' not in data:
                        logger.warning('[基础-API] 公告文档格式不正确')
                        return None
                    notice = data['announcement']
                    if notice is None:
                        return {}  # Explicit withdrawal; distinct from a failed request.
                    if not isinstance(notice, dict) or not notice.get('id'):
                        return None
                    if str(notice['id']) == str(current_id):
                        return None
                    data = {**notice, 'announcementId': notice['id']}
                    
                    # 如果返回空字典或无ID，也视为无更新
                    if not data or not data.get('announcementId'):
                        logger.info('[Base] 公告数据为空或无ID')
                        return None
                        
                    # 只要有标题，且有内容 OR 链接，就是有效公告
                    if data.get('title') and (data.get('content') or data.get('url')):
                        return data
                    else:
                        return None
                except json.JSONDecodeError as e:
                    logger.warning(f'[Base] 解析公告JSON失败: {e}, response={response_text[:100]}')
                    return None
            else:
                logger.warning(f'[Base] 获取公告失败: {response_text}')
                return None
                
        except Exception as e:
            logger.warning(f'[Base] 获取公告异常: {e}')
            return None

