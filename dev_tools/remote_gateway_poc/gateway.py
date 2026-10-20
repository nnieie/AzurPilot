"""Cloudflare 固定入口方案的最小 Gateway PoC（issue #1253 P0-a）。

验证目标只有一个：浏览器始终停留在 ``https://<gateway>/<device-id>/``，Gateway 把该前缀
下的 HTTP 与 WebSocket 流量转发给当前的隧道目标（本 PoC 直接指向本机 WebUI），而地址栏
不跳转到上游地址。

这不是生产实现：没有注册中心、设备鉴权、心跳、限流和隧道管理，也没有 TLS 终止与
Cloudflare 接入。它只回答 P0 的两个问题：

1. ``/<device-id>/`` 路径前缀下，SPA 静态资源、``/api/v1/ws`` 与 SPA 回退是否可用；
2. 转发时 ``Host``/``Origin`` 保持什么形态，WebUI 的 ``Origin == Host`` 校验才不会把
   远程会话误判为本机免密，也不会把外部请求误判为本机直连。

从仓库根目录运行 ``uv run python dev_tools/remote_gateway_poc/gateway.py``，
再访问 ``http://127.0.0.1:18080/demo/``；上游默认为本机 25548 端口的 WebUI。
可用 ``--device-id``、``--upstream``、``--listen`` 和 ``--port`` 调整试验入口。
"""
import argparse
import asyncio

from aiohttp import ClientSession, WSMsgType, web

# 逐跳头部不能跨代理转发。
HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
})

# 隧道标记头由本进程内的 P2P 隧道注入才可信；外部请求带来的一律丢弃。
UNTRUSTED_HEADERS = frozenset({'x-azurpilot-remote-access'})

# 转发时不透传的响应头：长度由本代理重新分块计算。
RESPONSE_SKIP = HOP_BY_HOP | {'content-length'}


def _forward_headers(request: web.Request) -> dict:
    """构造发往上游的请求头。

    关键点是**保留浏览器原始 Host**，而不是改写成上游的 ``127.0.0.1:25548``。
    WebUI 用 ``Host`` 是否回环来判定"本机免密"，改写会把公网访客变成管理员。

    Args:
        request: 浏览器发到 Gateway 的请求。

    Returns:
        dict: 可直接交给 aiohttp 客户端的头部字典。
    """
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP and name.lower() not in UNTRUSTED_HEADERS
    }
    headers['Host'] = request.host
    return headers


async def _relay_http(request: web.Request, session: ClientSession, url: str) -> web.StreamResponse:
    """转发普通 HTTP 请求，按流返回响应。

    Args:
        request: 浏览器请求。
        session: 共享的上游客户端会话。
        url: 上游完整地址（已剥离设备前缀）。

    Returns:
        StreamResponse: 已写出的流式响应。
    """
    body = request.content if request.can_read_body else None
    async with session.request(
        request.method,
        url,
        headers=_forward_headers(request),
        data=body,
        allow_redirects=False,
    ) as upstream:
        response = web.StreamResponse(status=upstream.status)
        for name, value in upstream.headers.items():
            if name.lower() not in RESPONSE_SKIP:
                response.headers.add(name, value)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response


async def _relay_websocket(request: web.Request, session: ClientSession, url: str) -> web.StreamResponse:
    """转发 WebSocket 升级与双向消息。

    ``Origin`` 必须原样透传：WebUI 在升级前校验 ``Origin`` 与 ``Host`` 同源，
    Gateway 丢掉 ``Origin`` 会让所有跨站脚本都能建立连接。

    Args:
        request: 浏览器 WebSocket 请求。
        session: 共享的上游客户端会话。
        url: 上游 WebSocket 地址。

    Returns:
        StreamResponse: 已完成的 WebSocket 响应。
    """
    headers = {'Host': request.host}
    for name in ('Origin', 'User-Agent', 'Accept-Language', 'Cookie'):
        if name in request.headers:
            headers[name] = request.headers[name]
    upstream = await session.ws_connect(url, headers=headers, timeout=30)
    browser = web.WebSocketResponse(autoping=True)
    await browser.prepare(request)

    async def pump(source, target, source_is_upstream: bool) -> None:
        async for message in source:
            if message.type == WSMsgType.TEXT:
                await (target.send_str(message.data))
            elif message.type == WSMsgType.BINARY:
                await target.send_bytes(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                await target.close(code=message.data or 1000, message=message.extra or '')
                return
            elif message.type == WSMsgType.ERROR:
                return

    tasks = [
        asyncio.create_task(pump(upstream, browser, True)),
        asyncio.create_task(pump(browser, upstream, False)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # 浏览器提前断开是常态，不向上抛。
            pass
    await upstream.close()
    return browser


def create_gateway(device_id: str, upstream: str) -> web.Application:
    """创建按设备前缀转发的 Gateway 应用。

    Args:
        device_id: 固定入口使用的设备 ID。
        upstream: 上游基地址，例如 ``http://127.0.0.1:25548``。

    Returns:
        web.Application: aiohttp 应用。
    """
    prefix = f'/{device_id}'
    base = upstream.rstrip('/')
    ws_base = 'ws' + base[len('http'):]

    async def handle(request: web.Request) -> web.StreamResponse:
        """处理 ``/<device-id>/...`` 下的全部 HTTP 与 WebSocket 请求。"""
        tail = request.path[len(prefix):] or '/'
        query = request.rel_url.query_string
        path = f'{tail}?{query}' if query else tail
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            try:
                return await _relay_websocket(request, request.app['session'], f'{ws_base}{path}')
            except Exception as error:  # 上游拒绝升级时返回 403，不把内部地址暴露给浏览器。
                raise web.HTTPForbidden(text=f'upstream refused websocket: {type(error).__name__}')
        return await _relay_http(request, request.app['session'], f'{base}{path}')

    async def slash_redirect(request: web.Request) -> web.Response:
        """补齐入口尾斜杠，避免相对资源丢掉设备前缀。"""
        raise web.HTTPMovedPermanently(location=f'{prefix}/')

    async def index_redirect(request: web.Request) -> web.Response:
        """访问根路径时引导到设备入口。"""
        raise web.HTTPNotFound(text='使用 /<device-id>/ 访问')

    async def on_startup(app: web.Application) -> None:
        app['session'] = ClientSession(auto_decompress=False)

    async def on_cleanup(app: web.Application) -> None:
        await app['session'].close()

    app = web.Application()
    app['device_id'] = device_id
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get(prefix, slash_redirect)
    app.router.add_route('*', f'{prefix}/{{tail:.*}}', handle)
    app.router.add_get('/', index_redirect)
    return app


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description='Cloudflare 固定入口 Gateway PoC')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--device-id', default='demo')
    parser.add_argument('--upstream', default='http://127.0.0.1:25548')
    args = parser.parse_args()
    web.run_app(
        create_gateway(args.device_id, args.upstream),
        host=args.listen,
        port=args.port,
        print=None,
    )


if __name__ == '__main__':
    main()
