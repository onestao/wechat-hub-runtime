#!/usr/bin/env python3
"""Private authenticated proxy in front of an attached Selkies process.

Selkies itself listens only on 127.0.0.1 inside the account's shared network
namespace.  This proxy is the only process listening on the Docker-internal
0.0.0.0:8081 endpoint.  The outer WeChat Hub Desktop Gateway supplies a
per-account secret header; browsers never see that secret and Selkies never
receives it, so Selkies startup/request logging cannot disclose it.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Any

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
except ImportError:  # pragma: no cover - Runtime image installs aiohttp
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]


TOKEN_HEADER = "X-WeChat-Hub-Desktop-Token"
TOKEN_PATH = Path("/run/secrets/wechat-hub-desktop-token")
UPSTREAM = "http://127.0.0.1:8082"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_BLOCKLIST = HOP_BY_HOP_HEADERS | {
    "host",
    "authorization",
    "cookie",
    TOKEN_HEADER.lower(),
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}
RESPONSE_BLOCKLIST = HOP_BY_HOP_HEADERS | {"set-cookie", "www-authenticate"}


def _configured_mib(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value)) * 1024 * 1024


def ws_limit() -> int:
    return _configured_mib("WECHAT_SELKIES_INTERNAL_MAX_WS_FRAME_MB", 64, minimum=4, maximum=512)


def http_limit() -> int:
    return _configured_mib("WECHAT_SELKIES_INTERNAL_MAX_HTTP_MB", 1024, minimum=32, maximum=4096)


def read_token() -> str:
    try:
        value = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("desktop token is unavailable") from exc
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise RuntimeError("desktop token is invalid")
    return value


def authorized(request: Any, expected: str) -> bool:
    supplied = str(request.headers.get(TOKEN_HEADER) or "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def request_headers(request: Any) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in request.headers.items()
        if str(key).lower() not in REQUEST_BLOCKLIST
    }


def client_protocols(request: Any) -> list[str]:
    raw = str(request.headers.get("Sec-WebSocket-Protocol") or "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def upstream_url(request: Any) -> str:
    path_qs = str(request.rel_url)
    if not path_qs.startswith("/"):
        path_qs = "/" + path_qs
    return UPSTREAM + path_qs


async def proxy_websocket(request: Any) -> Any:
    if web is None or ClientSession is None or ClientTimeout is None or WSMsgType is None:
        raise RuntimeError("aiohttp is required for Selkies internal gateway")
    protocols = client_protocols(request)
    browser = web.WebSocketResponse(
        protocols=protocols,
        autoping=False,
        heartbeat=None,
        compress=False,
        max_msg_size=ws_limit(),
    )
    await browser.prepare(request)
    timeout = ClientTimeout(total=None, sock_connect=5.0, sock_read=None)
    try:
        async with ClientSession(timeout=timeout) as client:
            try:
                upstream = await client.ws_connect(
                    upstream_url(request),
                    headers=request_headers(request),
                    protocols=protocols,
                    autoping=False,
                    heartbeat=None,
                    compress=0,
                    max_msg_size=ws_limit(),
                )
            except Exception:
                await browser.close(code=1011, message=b"desktop upstream unavailable")
                return browser

            async def browser_to_upstream() -> None:
                async for message in browser:
                    if message.type == WSMsgType.TEXT:
                        await upstream.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await upstream.send_bytes(message.data)
                    elif message.type == WSMsgType.PING:
                        await upstream.ping(message.data)
                    elif message.type == WSMsgType.PONG:
                        await upstream.pong(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            async def upstream_to_browser() -> None:
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        await browser.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await browser.send_bytes(message.data)
                    elif message.type == WSMsgType.PING:
                        await browser.ping(message.data)
                    elif message.type == WSMsgType.PONG:
                        await browser.pong(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            tasks = {
                asyncio.create_task(browser_to_upstream()),
                asyncio.create_task(upstream_to_browser()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
            await upstream.close()
    finally:
        if not browser.closed:
            await browser.close()
    return browser


async def proxy_http(request: Any) -> Any:
    if web is None or ClientSession is None or ClientTimeout is None:
        raise RuntimeError("aiohttp is required for Selkies internal gateway")
    timeout = ClientTimeout(total=None, sock_connect=5.0, sock_read=None)
    body = request.content.iter_chunked(64 * 1024) if request.can_read_body else None
    try:
        async with ClientSession(timeout=timeout) as client:
            async with client.request(
                request.method,
                upstream_url(request),
                headers=request_headers(request),
                data=body,
                allow_redirects=False,
            ) as upstream:
                headers = {
                    str(key): str(value)
                    for key, value in upstream.headers.items()
                    if str(key).lower() not in RESPONSE_BLOCKLIST
                }
                response = web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
    except Exception:
        raise web.HTTPBadGateway(text="Selkies desktop upstream is unavailable")


async def handler(request: Any) -> Any:
    if web is None:
        raise RuntimeError("aiohttp is required for Selkies internal gateway")
    expected = str(request.app["desktop_token"])
    if not authorized(request, expected):
        # Deliberately do not disclose whether a token was missing or wrong.
        raise web.HTTPNotFound(text="Desktop endpoint is unavailable")
    if str(request.headers.get("Upgrade") or "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


def create_app(token: str | None = None) -> Any:
    if web is None:
        raise RuntimeError("aiohttp is required for Selkies internal gateway")
    app = web.Application(client_max_size=http_limit())
    app["desktop_token"] = token or read_token()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main() -> int:
    if web is None:
        raise RuntimeError("aiohttp is required for Selkies internal gateway")
    host = os.environ.get("WECHAT_SELKIES_INTERNAL_BIND", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("WECHAT_SELKIES_INTERNAL_PORT", "8081"))
    except ValueError:
        port = 8081
    web.run_app(create_app(), host=host, port=port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
