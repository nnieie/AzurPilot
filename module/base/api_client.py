"""API 客户端模块。

负责与 API 服务器进行 HTTP 交互，包括 Bug 日志上报、CL1 数据提交和公告获取，
支持主域名 (nanoda.work) 和备用域名 (nanoda.work) 的自动故障转移。
"""
import threading
from typing import Any, Dict, List, Tuple, Optional

import requests

from module.base.device_id import get_device_id
from module.logger import logger


class ApiClient:
    """统一的 API 客户端，支持双域名故障转移。"""
    
    # 主域名和备用域名列表
    PRIMARY_DOMAIN = 'https://alas-apiv2.nanoda.work'
    FALLBACK_DOMAIN = 'https://alas-apiv2.nanoda.work'
    
    # API端点路径
    BUG_LOG_PATH = '/api/post/bug'
    CL1_DATA_PATH = '/api/telemetry'
    ANNOUNCEMENT_PATH = '/api/get/announcement'

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
        return [
            f'{cls.PRIMARY_DOMAIN}{path}',
            f'{cls.FALLBACK_DOMAIN}{path}'
        ]
    
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
                            'User-Agent': 'alas AzurPilot'
                        }
                    )
                
                if response.status_code in success_codes:
                    if i > 0:
                        logger.info(f'[基础-API] 使用{domain_type}请求成功')
                    return True, response.status_code, response.text
                else:
                    logger.warning(f'[基础-API] {domain_type}返回错误状态: {response.status_code}')
                    last_error = f'HTTP {response.status_code}'
                    
            except requests.exceptions.Timeout:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}请求超时')
                last_error = 'Timeout'
            except requests.exceptions.RequestException as e:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}请求失败: {e}')
                last_error = str(e)
            except Exception as e:
                logger.warning(f'[基础-API] {domain_type if i > 0 else "主域名"}发生异常: {e}')
                last_error = str(e)
        
        return False, 0, last_error or 'Unknown error'
    
    @staticmethod
    def _submit_bug_log(content: str, log_type: str):
        """内部方法：提交 Bug 日志。
        
        注：服务端 API 若废弃则降级记录警告日志。

        Args:
            content (str): 日志文本内容。
            log_type (str): 日志级别/类型。
        """
        try:
            device_id = get_device_id()
            data = {
                'device_id': device_id,
                'log_type': log_type,
                'log_content': content,
            }
            
            success, status_code, response_text = ApiClient._post_with_fallback(
                ApiClient.BUG_LOG_PATH,
                data,
                timeout=5
            )
            
            if success:
                logger.info(f'[基础-API] Bug日志已提交: {content[:50]}...')
            else:
                logger.warning(f'[基础-API] 提交Bug日志失败: {response_text}')
        except Exception as e:
            logger.warning(f'[基础-API] 提交Bug日志失败: {e}')
    
    @classmethod
    def submit_bug_log(cls, content: str, log_type: str = 'warning', enabled: bool = True):
        """提交 Bug 日志（异步执行）。
        
        Args:
            content (str): 日志内容。
            log_type (str): 日志类型，默认为 'warning'。
            enabled (bool): 是否启用上报，可传入 config.DropRecord_BugReport 配置值。
        """
        if not enabled:
            return
        from module.base.async_executor import async_executor
        async_executor.submit(cls._submit_bug_log, content, log_type)
    
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
            
            success, status_code, response_text = ApiClient._post_with_fallback(
                ApiClient.CL1_DATA_PATH,
                data,
                timeout=timeout
            )
            
            if success:
                logger.info('[基础-API] CL1 数据提交成功')
            else:
                logger.warning(f'[基础-API] CL1 数据提交失败: {response_text}')

        except Exception as e:
            logger.exception(f'[基础-API] CL1 数据提交异常: {e}')
    
    @classmethod
    def submit_cl1_data(cls, data: Dict[str, Any], timeout: int = 10):
        """提交 CL1 统计数据（异步执行）。

        仅上传经过哈希处理的设备 ID 与统计指标，不包含原始硬件敏感信息。

        Args:
            data (dict[str, Any]): 包含 device_id 和统计数据的字典。
            timeout (int): 请求超时时间（秒），默认 10 秒。
        """
        from module.base.async_executor import async_executor
        async_executor.submit(cls._submit_cl1_data, data, timeout)

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

