"""远程配对的 WebSocket API；复用 Quick Tunnel 的设备身份。"""

import asyncio

from module.api.protocol import ApiError
from module.runtime.remote_cloudflare import cloudflare, RemoteRegistryError


def create_pairing():
    """创建服务器生成的配对码，不向前端返回访问凭据。"""
    try:
        return cloudflare.create_pairing()
    except (ValueError, TimeoutError) as exc:
        raise ApiError('REMOTE_UNAVAILABLE', str(exc)) from exc


def pairing_status(pairing_id):
    """查询配对是否已被 AFA 领取。"""
    try:
        return asyncio.run(cloudflare.request('GET', '/api/v1/pairings/' + pairing_id))
    except RemoteRegistryError as exc:
        if exc.code == 'PAIRING_EXPIRED':
            return {'state': 'expired'}
        raise ApiError('REMOTE_UNAVAILABLE', str(exc)) from exc
    except (ValueError, TimeoutError) as exc:
        raise ApiError('REMOTE_UNAVAILABLE', str(exc)) from exc


def clients():
    """仅返回配对元数据，避免暴露凭据摘要。"""
    store = cloudflare.identity()
    if not store.path.exists():
        return []
    return [{'id': item['id'], 'created_at': item['created_at']} for item in store.load()['clients']]


def revoke(client_id):
    """撤销一个 AFA 客户端的后续鉴权权限。"""
    cloudflare.identity().remove_client(client_id)
    return {'revoked': True}
