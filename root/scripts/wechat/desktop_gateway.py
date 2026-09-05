#!/usr/bin/env python3
"""Account-scoped reverse proxy for agent-wechat's browser desktop.

The browser never connects to an agent-wechat child container directly. It
uses a short-lived opaque gateway session created by Runtime. This process
resolves that session back to exactly one Runtime account and injects the
upstream bearer token only on the internal Docker-network hop.

HTTP and WebSocket traffic are both proxied because noVNC loads normal web
assets first and then upgrades a long-lived binary WebSocket (websockify).
Access logging is deliberately disabled so gateway session identifiers do not
become durable log credentials. The upstream token never appears in browser
URLs, public JSON, redirects, or logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:  # Linux production path; Windows unit tests use the process-local map.
    import fcntl
except ImportError:  # pragma: no cover - platform-specific fallback.
    fcntl = None

try:
    from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
except ImportError:  # pragma: no cover - production image installs aiohttp
    ClientSession = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

from agent_wechat_runtime import AGENT_WECHAT_PORT, SELKIES_ATTACH_PORT, AgentWechatManager
from wechat_runtime import Registry, RuntimePaths, find_account, runtime_provider


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
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
REQUEST_HEADER_BLOCKLIST = HOP_BY_HOP_HEADERS | {
    "authorization",
    "cookie",
    "host",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}
RESPONSE_HEADER_BLOCKLIST = HOP_BY_HOP_HEADERS | {"set-cookie", "www-authenticate"}
SELKIES_WEB_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
}
# The base image keeps shared branding assets next to the dashboard bundle,
# not inside it, so the Gateway falls back to that directory for them.
SELKIES_WEB_FALLBACK_FILES = ("icon.png", "favicon.ico")


class GatewaySessionError(RuntimeError):
    pass


_MANUAL_GUI_LEASES: dict[str, dict[str, Any]] = {}
_MANUAL_GUI_LEASES_GUARD = threading.Lock()
_IDLE_CLEANUP_TIMERS: dict[str, Any] = {}


def _cleanup_idle_selkies_companion(account_key: str) -> None:
    try:
        manager = AgentWechatManager()
        account = {"id": account_key}
        manager._remove_selkies_container(account)
    except Exception:
        pass


def cancel_idle_selkies_cleanup(account_key: str) -> None:
    timer = _IDLE_CLEANUP_TIMERS.pop(str(account_key), None)
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def schedule_idle_selkies_cleanup(account_key: str, delay_seconds: float | None = None) -> None:
    cancel_idle_selkies_cleanup(account_key)
    if delay_seconds is None:
        try:
            delay_seconds = max(0.0, float(os.environ.get("WECHAT_SELKIES_IDLE_TTL_SECONDS", "10.0")))
        except ValueError:
            delay_seconds = 10.0

    if delay_seconds <= 0.0:
        _cleanup_idle_selkies_companion(account_key)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        _cleanup_idle_selkies_companion(account_key)
        return

    def _fire():
        _IDLE_CLEANUP_TIMERS.pop(str(account_key), None)
        with _MANUAL_GUI_LEASES_GUARD:
            if str(account_key) in _MANUAL_GUI_LEASES:
                return
        _cleanup_idle_selkies_companion(account_key)

    _IDLE_CLEANUP_TIMERS[str(account_key)] = loop.call_later(delay_seconds, _fire)


def _account_gui_lease_path(account_id: str) -> Path:
    root = Path(os.environ.get("WECHAT_GUI_LEASE_DIR", "/run/wechat-runtime/locks"))
    digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:32]
    return root / f"account-gui-{digest}.lock"


def acquire_manual_gui_lease(account_id: str, session_id: str) -> bool:
    """Reserve one account GUI for a browser desktop control session.

    Selkies can establish more than one WebSocket for one page, so the first
    connection owns the cross-process flock and later connections from the
    same opaque session only increase a reference count. A second browser
    session for the same account is refused while the first is active.
    """

    account_key = str(account_id)
    with _MANUAL_GUI_LEASES_GUARD:
        existing = _MANUAL_GUI_LEASES.get(account_key)
        if existing is not None:
            if str(existing.get("session_id") or "") != str(session_id):
                return False
            existing["count"] = int(existing.get("count") or 0) + 1
            return True

        path = _account_gui_lease_path(account_key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
        except OSError:
            return False
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            except OSError:
                handle.close()
                return False
        cancel_idle_selkies_cleanup(account_key)
        _MANUAL_GUI_LEASES[account_key] = {
            "session_id": str(session_id),
            "count": 1,
            "handle": handle,
        }
        return True


def release_manual_gui_lease(account_id: str, session_id: str) -> None:
    account_key = str(account_id)
    handle = None
    last_session_closed = False
    with _MANUAL_GUI_LEASES_GUARD:
        existing = _MANUAL_GUI_LEASES.get(account_key)
        if existing is None or str(existing.get("session_id") or "") != str(session_id):
            return
        count = int(existing.get("count") or 0) - 1
        if count > 0:
            existing["count"] = count
            return
        handle = existing.get("handle")
        _MANUAL_GUI_LEASES.pop(account_key, None)
        last_session_closed = True
    if handle is not None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    if last_session_closed:
        schedule_idle_selkies_cleanup(account_key)


def _configured_mib(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value)) * 1024 * 1024


def websocket_frame_limit() -> int:
    # Large clipboard images/file-transfer chunks need more than aiohttp's
    # 4 MiB WebSocket default, while a finite bound still limits abuse.
    return _configured_mib("WECHAT_DESKTOP_GATEWAY_MAX_WS_FRAME_MB", 64, minimum=4, maximum=512)


def http_request_limit() -> int:
    # HTTP uploads are streamed, not buffered by this proxy.  Keep a generous
    # configurable ceiling for Selkies file transfers.
    return _configured_mib("WECHAT_DESKTOP_GATEWAY_MAX_HTTP_MB", 1024, minimum=32, maximum=4096)


def session_dir() -> Path:
    return Path(
        os.environ.get("WECHAT_DESKTOP_GATEWAY_SESSION_DIR", "/run/wechat-runtime/desktop-sessions")
    )


def load_session(session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve an opaque session to exactly one live AgentWechat account."""

    if not SESSION_ID_RE.fullmatch(session_id):
        raise GatewaySessionError("invalid desktop session")
    path = session_dir() / f"{session_id}.json"
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GatewaySessionError("desktop session not found") from exc
    if not isinstance(descriptor, dict):
        raise GatewaySessionError("desktop session is invalid")
    try:
        expires_at = int(descriptor.get("expires_at") or 0)
    except (TypeError, ValueError) as exc:
        raise GatewaySessionError("desktop session is invalid") from exc
    if expires_at < int(time.time()):
        path.unlink(missing_ok=True)
        raise GatewaySessionError("desktop session expired")
    account_id = str(descriptor.get("account_id") or "").strip()
    if not account_id:
        raise GatewaySessionError("desktop session is invalid")
    provider = str(descriptor.get("desktop_provider") or "novnc").strip().lower()
    if provider not in {"novnc", "selkies"}:
        raise GatewaySessionError("desktop session provider is invalid")

    registry = Registry(RuntimePaths.from_env())
    data = registry.load(create=False)
    try:
        account = find_account(data, account_id)
    except Exception as exc:
        raise GatewaySessionError("desktop account is no longer registered") from exc
    if runtime_provider(account) != "agent_wechat":
        raise GatewaySessionError("desktop session provider changed")
    return descriptor, account


def client_protocols(request: Any) -> list[str]:
    raw = str(request.headers.get("Sec-WebSocket-Protocol") or "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def upstream_url(
    account: dict[str, Any], tail: str, query: Any, *, websocket: bool = False
) -> str:
    """Build an internal-only URL and inject WebSocket auth server-side."""

    manager = AgentWechatManager()
    params: list[tuple[str, str]] = []
    for key, value in query.items():
        if str(key).lower() == "token":
            continue
        params.append((str(key), str(value)))
    # HTTP requests authenticate with the Authorization header. Adding the
    # token to those URLs lets upstream noVNC reflect it into browser-visible
    # redirects/history. Only upstream WebSockets require query auth.
    if websocket:
        params.append(("token", manager._token(account)))
    path = "/" + tail.lstrip("/")
    encoded = urllib.parse.urlencode(params)
    suffix = f"?{encoded}" if encoded else ""
    return f"http://{manager.container_name(account)}:{AGENT_WECHAT_PORT}{path}{suffix}"


def upstream_headers(request: Any, account: dict[str, Any]) -> dict[str, str]:
    headers = {
        str(key): str(value)
        for key, value in request.headers.items()
        if str(key).lower() not in REQUEST_HEADER_BLOCKLIST
    }
    headers["Authorization"] = f"Bearer {AgentWechatManager()._token(account)}"
    return headers


def selkies_upstream_url(account: dict[str, Any], tail: str, query: Any) -> str:
    """Build the internal URL for the account's display-only Selkies companion."""

    params = [
        (str(key), str(value))
        for key, value in query.items()
        if str(key).lower() not in {"token", "authorization"}
    ]
    path = "/" + tail.lstrip("/")
    encoded = urllib.parse.urlencode(params)
    suffix = f"?{encoded}" if encoded else ""
    return (
        f"http://{AgentWechatManager.container_name(account)}:"
        f"{SELKIES_ATTACH_PORT}{path}{suffix}"
    )


def desktop_provider(descriptor: dict[str, Any]) -> str:
    provider = str(descriptor.get("desktop_provider") or "novnc").strip().lower()
    return "selkies" if provider == "selkies" else "novnc"


def selkies_web_root() -> Path:
    """Filesystem root of the Selkies browser client bundled in the Runtime image."""

    return Path(
        os.environ.get("WECHAT_SELKIES_WEB_ROOT", "/usr/share/selkies/selkies-dashboard")
    )


def selkies_web_fallback_root() -> Path:
    return Path(
        os.environ.get("WECHAT_SELKIES_WEB_FALLBACK_ROOT", "/usr/share/selkies/www")
    )


def _safe_web_file(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` below ``root`` and fail closed on any escape.

    Component checks, canonical resolution, and a strict containment check
    must all agree before a file is handed to the browser. A symlink inside
    the web root that resolves outside it is rejected like a ``..`` escape.
    """

    parts = Path(relative).parts
    if not parts or any(part in {"..", ""} for part in parts):
        return None
    try:
        resolved_root = root.resolve()
        candidate = (root / relative).resolve()
    except OSError:
        return None
    if resolved_root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate


def resolve_selkies_web_file(tail: str) -> Path | None:
    """Resolve a browser tail to the bundled Selkies web client.

    The raw Selkies companion answers plain HTTP with 426 Upgrade Required,
    so the navigable client (HTML/JS/CSS) has to come from this Gateway. Any
    tail that does not resolve to a bundled file returns None and falls
    through to the upstream proxy path.
    """

    root = selkies_web_root()
    if not root.is_dir():
        return None
    relative = tail.strip("/")
    if not relative:
        relative = "index.html"
    candidate = _safe_web_file(root, relative)
    if candidate is not None:
        return candidate
    if relative in SELKIES_WEB_FALLBACK_FILES:
        return _safe_web_file(selkies_web_fallback_root(), relative)
    return None


def selkies_web_content_type(path: Path) -> str | None:
    return SELKIES_WEB_CONTENT_TYPES.get(path.suffix.lower())


async def serve_selkies_web_file(path: Path) -> Any:
    assert web is not None
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    content_type = selkies_web_content_type(path)
    if content_type:
        headers["Content-Type"] = content_type
    return web.FileResponse(path, headers=headers)


def selkies_web_manifest_response(tail: str) -> Any:
    """Serve the PWA manifest that the nginx-based stack normally generates."""

    assert web is not None
    if tail.strip("/") != "manifest.json":
        return None
    title = os.environ.get("WECHAT_SELKIES_UI_TITLE", "微信桌面").strip() or "微信桌面"
    return web.json_response(
        {"name": title, "short_name": title, "display": "fullscreen", "start_url": "./"},
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def proxy_upstream_url(
    descriptor: dict[str, Any],
    account: dict[str, Any],
    tail: str,
    query: Any,
    *,
    websocket: bool = False,
) -> str:
    if desktop_provider(descriptor) == "selkies":
        return selkies_upstream_url(account, tail, query)
    return upstream_url(account, tail, query, websocket=websocket)


def proxy_upstream_headers(
    request: Any,
    descriptor: dict[str, Any],
    account: dict[str, Any],
    session_id: str,
) -> dict[str, str]:
    if desktop_provider(descriptor) != "selkies":
        return upstream_headers(request, account)
    headers = {
        str(key): str(value)
        for key, value in request.headers.items()
        if str(key).lower() not in REQUEST_HEADER_BLOCKLIST
    }
    # The browser never sees the independent per-account Selkies credential.
    # The opaque WeChat Hub gateway session is the browser authorization
    # boundary; the secret header exists only on this Docker-internal hop.
    # Supplying the prefix keeps assets/WebSockets below the account route.
    headers["X-Forwarded-Prefix"] = f"/desktop/{session_id}/"
    headers["X-Forwarded-Proto"] = str(getattr(request, "scheme", "http") or "http")
    headers["X-WeChat-Hub-Desktop-Token"] = AgentWechatManager()._desktop_token(account)
    return headers


def landing_tail(tail: str) -> str:
    """Point the browser at the real noVNC client instead of upstream's gate.

    agent-wechat serves a hand-rolled "Access Token" prompt for ``/vnc/``
    whenever the query string has no ``token=`` parameter, and it only renders
    the actual noVNC client when the upstream token is echoed into the page and
    into the browser-visible WebSocket URL.  That token must never leave the
    internal Docker hop, so the Gateway asks upstream for ``vnc.html``
    directly.  It is the same client, it takes its connection settings from the
    opaque ``path`` parameter that the descriptor already supplies, and it needs
    no body rewriting, so Content-Length stays authoritative.
    """

    return "vnc/vnc.html" if tail.strip("/").lower() == "vnc" else tail


def rewrite_location(
    location: str,
    session_id: str,
    account: dict[str, Any],
    *,
    provider: str = "novnc",
) -> str:
    """Keep upstream redirects inside the same opaque gateway session."""

    if not location:
        return location
    parsed = urllib.parse.urlsplit(location)
    if provider == "selkies":
        upstream_host = f"{AgentWechatManager.container_name(account)}:{SELKIES_ATTACH_PORT}"
    else:
        upstream_host = f"{AgentWechatManager.container_name(account)}:{AGENT_WECHAT_PORT}"
    if parsed.netloc and parsed.netloc != upstream_host:
        return ""
    path = parsed.path or "/"
    query: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "token":
            continue
        if provider != "selkies" and key.lower() == "path":
            nested = urllib.parse.urlsplit(value)
            nested_path = nested.path.lstrip("/")
            if nested_path.endswith("websockify"):
                # agent-wechat canonicalizes noVNC redirects to a path value
                # that embeds its upstream token. Browser WebSockets must stay
                # on the opaque account-scoped Gateway route instead.
                value = f"desktop/{session_id}/vnc/websockify"
            elif nested.query:
                nested_query = [
                    (nested_key, nested_value)
                    for nested_key, nested_value in urllib.parse.parse_qsl(
                        nested.query, keep_blank_values=True
                    )
                    if nested_key.lower() != "token"
                ]
                value = urllib.parse.urlunsplit(
                    (nested.scheme, nested.netloc, nested.path, urllib.parse.urlencode(nested_query), nested.fragment)
                )
        query.append((key, value))
    suffix = urllib.parse.urlencode(query)
    rewritten = f"/desktop/{session_id}{path}"
    rewritten += f"?{suffix}" if suffix else ""

    # Redirects are the only upstream-controlled browser navigation surface.
    # Fail closed if a future upstream format evades the structured cleanup.
    decoded = urllib.parse.unquote_plus(rewritten)
    token = AgentWechatManager()._token(account) if provider != "selkies" else ""
    if (token and token in decoded) or re.search(r"(?:^|[?&])token=", decoded, flags=re.IGNORECASE):
        return ""
    return rewritten


async def proxy_websocket(
    request: Any,
    descriptor: dict[str, Any],
    session_id: str,
    account: dict[str, Any],
    tail: str,
) -> Any:
    assert web is not None and ClientSession is not None and ClientTimeout is not None and WSMsgType is not None
    account_id = str(account.get("id") or "")
    if not acquire_manual_gui_lease(account_id, session_id):
        # Core Sender takes the same account-scoped flock while driving the
        # upstream GUI. Refuse the browser control channel rather than letting
        # manual navigation race a live send into the wrong chat.
        raise web.HTTPServiceUnavailable(
            text="WeChat desktop is temporarily busy with an automated action",
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    protocols = client_protocols(request)
    browser = web.WebSocketResponse(
        protocols=protocols,
        autoping=False,
        heartbeat=None,
        compress=False,
        max_msg_size=websocket_frame_limit(),
    )
    try:
        await browser.prepare(request)
    except Exception:
        release_manual_gui_lease(account_id, session_id)
        raise

    timeout = ClientTimeout(total=None, sock_connect=8.0, sock_read=None)
    try:
        async with ClientSession(timeout=timeout) as client:
            try:
                upstream = await client.ws_connect(
                    proxy_upstream_url(
                        descriptor, account, tail, request.query, websocket=True
                    ),
                    headers=proxy_upstream_headers(request, descriptor, account, session_id),
                    protocols=protocols,
                    autoping=False,
                    heartbeat=None,
                    compress=0,
                    max_msg_size=websocket_frame_limit(),
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
        release_manual_gui_lease(account_id, session_id)
    return browser


async def proxy_http(
    request: Any,
    descriptor: dict[str, Any],
    session_id: str,
    account: dict[str, Any],
    tail: str,
) -> Any:
    assert web is not None and ClientSession is not None and ClientTimeout is not None
    timeout = ClientTimeout(total=None, sock_connect=8.0, sock_read=None)
    body = request.content.iter_chunked(64 * 1024) if request.can_read_body else None
    try:
        async with ClientSession(timeout=timeout) as client:
            effective_tail = landing_tail(tail) if desktop_provider(descriptor) == "novnc" else tail
            async with client.request(
                request.method,
                proxy_upstream_url(descriptor, account, effective_tail, request.query),
                headers=proxy_upstream_headers(request, descriptor, account, session_id),
                data=body,
                allow_redirects=False,
            ) as upstream:
                response_headers: dict[str, str] = {}
                for key, value in upstream.headers.items():
                    lower = key.lower()
                    if lower in RESPONSE_HEADER_BLOCKLIST or lower == "location":
                        continue
                    response_headers[key] = value
                # The opaque gateway session lives in the browser URL. Avoid
                # caches/referrers persisting or forwarding that capability.
                response_headers["Cache-Control"] = "no-store, max-age=0"
                response_headers["Pragma"] = "no-cache"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["X-Content-Type-Options"] = "nosniff"
                location = rewrite_location(
                    str(upstream.headers.get("Location") or ""),
                    session_id,
                    account,
                    provider=desktop_provider(descriptor),
                )
                if location:
                    response_headers["Location"] = location
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
    except Exception:
        raise web.HTTPBadGateway(text="AgentWechat desktop upstream is unavailable")


async def desktop_handler(request: Any) -> Any:
    assert web is not None
    session_id = str(request.match_info.get("session") or "")
    tail = str(request.match_info.get("tail") or "")
    try:
        descriptor, account = load_session(session_id)
    except GatewaySessionError:
        raise web.HTTPNotFound(text="Desktop session is unavailable")
    if str(request.headers.get("Upgrade") or "").lower() == "websocket":
        return await proxy_websocket(request, descriptor, session_id, account, tail)
    if desktop_provider(descriptor) == "selkies" and str(request.method).upper() == "GET":
        local_file = resolve_selkies_web_file(tail)
        if local_file is not None:
            return await serve_selkies_web_file(local_file)
        if not tail.strip("/"):
            # The landing page must always be navigable HTML. The raw Selkies
            # companion only answers plain HTTP with 426 Upgrade Required, so
            # a missing client bundle is a Runtime packaging failure, not
            # something the upstream can serve.
            raise web.HTTPServiceUnavailable(
                text="WeChat Hub desktop client is unavailable in this Runtime image",
                headers={"Cache-Control": "no-store"},
            )
        manifest = selkies_web_manifest_response(tail)
        if manifest is not None:
            return manifest
    return await proxy_http(request, descriptor, session_id, account, tail)


async def desktop_session_redirect(request: Any) -> Any:
    """Normalize ``/desktop/<session>`` to the client's trailing-slash path."""

    assert web is not None
    session_id = str(request.match_info.get("session") or "")
    location = f"/desktop/{session_id}/"
    # Reflect nothing credential-shaped back into a browser-visible redirect.
    query = [
        (str(key), str(value))
        for key, value in request.query.items()
        if str(key).lower() != "token"
    ]
    if query:
        location += "?" + urllib.parse.urlencode(query)
    raise web.HTTPFound(location)


async def health_handler(_request: Any) -> Any:
    assert web is not None
    return web.json_response({"ok": True, "service": "wechat-desktop-gateway"})


def create_app() -> Any:
    if web is None:
        raise RuntimeError("aiohttp is required for WeChat Desktop Gateway")
    app = web.Application(client_max_size=http_request_limit())
    app.router.add_get("/healthz", health_handler)
    app.router.add_get("/desktop/{session}", desktop_session_redirect)
    app.router.add_route("*", "/desktop/{session}/{tail:.*}", desktop_handler)
    return app


def main() -> int:
    if web is None:
        raise RuntimeError("aiohttp is required for WeChat Desktop Gateway")
    host = os.environ.get("WECHAT_DESKTOP_GATEWAY_BIND", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("WECHAT_DESKTOP_GATEWAY_PORT", "17892"))
    except ValueError:
        port = 17892
    web.run_app(create_app(), host=host, port=port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
