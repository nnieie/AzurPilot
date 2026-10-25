"""本机任务通知入口，后端统一分发给启动器 WS 与浏览器通知中心。"""
from pydantic import ValidationError
from starlette.responses import JSONResponse
from starlette.routing import Route

from module.api.launcher_protocol import NotificationParams
from module.api.launcher_api import removed
from module.runtime.launcher import is_local_request
from module.runtime.notify_hub import hub


async def notify(request):
    if not is_local_request(request):
        return JSONResponse({'ok': False, 'error': '通知接收只允许本机调用'}, status_code=403)
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        return JSONResponse({'ok': False, 'error': 'Content-Type 必须是 application/json'}, status_code=400)
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return JSONResponse({'ok': False, 'error': '通知超过大小限制'}, status_code=413)
        notification = NotificationParams.model_validate_json(body).model_dump()
    except (ValidationError, ValueError):
        return JSONResponse({'ok': False, 'error': '通知格式无效'}, status_code=400)
    request.app.state.gateway.router.launcher.publish(notification)
    hub.publish(notification)
    return JSONResponse({'ok': True})


def routes():
    return [Route('/api/v2/internal/notifications', notify, methods=['POST']),
            Route('/api/notify', removed, methods=['GET', 'POST']),
            Route('/api/notify_stream', removed)]
