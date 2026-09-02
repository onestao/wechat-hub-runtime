#!/usr/bin/env python3
"""Root-side Unix-socket control plane for the multi-account Runtime.

The socket lives only on Runtime's shared state volume.  Core can therefore
request account lifecycle operations without Docker socket access, while the
privileged Unix-user/process operations stay inside the Runtime container.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socketserver
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from wechat_runtime import (
    Registry,
    RuntimeErrorWithHint,
    RuntimePaths,
    find_account,
    register_account,
    restart_account,
    runtime_provider,
    start_account,
    status_for,
    stop_account,
    unregister_account,
    account_environment,
    user_exec_prefix,
)


CONTROL_LOCK = threading.RLock()
DEFAULT_SOCKET = "/run/wechat-runtime/control.sock"
CAPTURE_HELPER = "/scripts/wechat/wechat-window-capture.py"
LIST_STATUS_WORKERS = 8
LIST_AGENT_PROBE_TIMEOUT = 1.25


def _preferred_window(status: dict[str, Any]) -> dict[str, Any] | None:
    windows = [item for item in status.get("windows") or [] if isinstance(item, dict)]
    if not windows:
        return None
    return max(
        windows,
        key=lambda item: max(0, int(item.get("width") or 0)) * max(0, int(item.get("height") or 0)),
    )


def login_status_for(account: dict[str, Any]) -> dict[str, Any]:
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        return AgentWechatManager().login_status(account)
    status = status_for(account)
    selected = _preferred_window(status)
    return {
        "account_id": account["id"],
        "display_name": str(account.get("display_name") or account["id"]),
        "running": bool(status.get("running")),
        "pids": list(status.get("pids") or []),
        "windows": list(status.get("windows") or []),
        "snapshot_available": bool(status.get("running") and selected),
        "window_id": selected.get("window_id") if selected else None,
        "window_title": str(selected.get("title") or "") if selected else "",
    }


def capture_login_window(account: dict[str, Any]) -> dict[str, Any]:
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        return AgentWechatManager().capture_login(account)
    status = login_status_for(account)
    if not status["running"]:
        raise RuntimeErrorWithHint("WeChat account is stopped")
    if not status["snapshot_available"] or status["window_id"] is None:
        raise RuntimeErrorWithHint("WeChat login window is not ready yet")
    python_bin = "/lsiopy/bin/python3" if Path("/lsiopy/bin/python3").is_file() else sys.executable
    account_command = user_exec_prefix(account) + [
        python_bin,
        CAPTURE_HELPER,
        "--window-id",
        str(status["window_id"]),
        "--max-width",
        "960",
        "--max-height",
        "820",
    ]
    command = [
        "/scripts/wechat/wechat-display-lock.sh",
        account["id"],
        *account_command,
    ]
    try:
        content = subprocess.check_output(
            command,
            env=account_environment(account),
            stderr=subprocess.PIPE,
            timeout=8,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeErrorWithHint(detail or "Unable to capture WeChat login window") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeErrorWithHint("WeChat login window capture timed out") from exc
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeErrorWithHint("WeChat login window capture returned invalid PNG data")
    if len(content) > 1400 * 1024:
        raise RuntimeErrorWithHint("WeChat login window snapshot is unexpectedly large")
    return {
        "account_id": account["id"],
        "content_type": "image/png",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "window_id": status["window_id"],
    }


def _bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "invalid_request"
    message = str(exc).lower()
    if "already exists" in message:
        return "account_exists"
    if "unknown wechat account" in message:
        return "account_not_found"
    return "runtime_operation_failed"


def _list_status_for(account: dict[str, Any]) -> dict[str, Any]:
    """Fetch one status with list-specific short upstream probe timeouts."""

    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        return AgentWechatManager().status(account, probe_timeout=LIST_AGENT_PROBE_TIMEOUT)
    return status_for(account)


def _degraded_list_status(account: dict[str, Any], exc: Exception) -> dict[str, Any]:
    provider = runtime_provider(account)
    return {
        "account_id": str(account["id"]),
        "display_name": str(account.get("display_name") or account["id"]),
        "runtime_provider": provider,
        "enabled": bool(account.get("enabled", True)),
        "autostart": bool(account.get("autostart", True)),
        "legacy": bool(account.get("legacy", provider == "legacy")),
        "running": None,
        "container_running": None if provider == "agent_wechat" else False,
        "agent_server_healthy": False if provider == "agent_wechat" else None,
        "runtime_health": "degraded",
        "wechat_login_status": "unknown" if provider == "agent_wechat" else "unknown",
        "pids": [],
        "windows": [],
        "health_error": str(exc),
    }


def list_account_statuses(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not accounts:
        return []
    workers = max(1, min(LIST_STATUS_WORKERS, len(accounts)))
    results: list[dict[str, Any] | None] = [None] * len(accounts)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="runtime-status") as pool:
        futures = {pool.submit(_list_status_for, account): index for index, account in enumerate(accounts)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = _degraded_list_status(accounts[index], exc)
    return [item for item in results if item is not None]


def dispatch_action(registry: Registry, request: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get("action") or "").strip().lower()
    if not action:
        raise ValueError("action is required")

    # Listing is the one control operation that deliberately releases the
    # registry lock before any per-account status probes.  A frozen
    # agent-server therefore cannot hold the global Runtime control lock or
    # serialize otherwise healthy accounts behind its network timeout.
    if action == "list":
        with CONTROL_LOCK:
            data = registry.load(create=False)
            accounts = json.loads(json.dumps(data["accounts"]))
        return {"accounts": list_account_statuses(accounts)}

    with CONTROL_LOCK:
        if action == "health":
            data = registry.load(create=False)
            return {"ok": True, "accounts": len(data["accounts"])}

        account_id = str(request.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("account_id is required")

        if action == "register":
            account = register_account(
                registry,
                account_id,
                str(request.get("display") or "").strip() or None,
                _bool(request, "autostart", True),
                str(request.get("display_name") or "").strip() or None,
                str(request.get("runtime_provider") or request.get("provider") or "legacy"),
            )
            if _bool(request, "start", True):
                return {"account": account, "status": start_account(account, registry.paths)}
            return {"account": account, "status": status_for(account)}

        data = registry.load(create=False)
        account = find_account(data, account_id)
        if action == "start":
            return {"status": start_account(account, registry.paths)}
        if action == "stop":
            return {"status": stop_account(account, registry.paths)}
        if action == "restart":
            return {"status": restart_account(account, registry.paths)}
        if action == "unregister":
            return unregister_account(
                registry,
                account_id,
                purge_data=_bool(request, "purge_data", False),
            )
        if action == "status":
            return {"status": status_for(account)}
        if action == "login_status":
            return {"login": login_status_for(account)}
        if action == "start_login":
            if runtime_provider(account) == "agent_wechat":
                from agent_wechat_runtime import AgentWechatManager

                return {"login": AgentWechatManager().start_login(account)}
            return {"login": login_status_for(account)}
        if action == "capture_login":
            return capture_login_window(account)
        if action == "desktop":
            if runtime_provider(account) == "agent_wechat":
                from agent_wechat_runtime import AgentWechatManager

                return {
                    "desktop": AgentWechatManager().desktop(
                        account,
                        desktop_provider=str(request.get("desktop_provider") or "auto"),
                    )
                }
            return {
                "desktop": {
                    "account_id": account_id,
                    "runtime_provider": "legacy",
                    "path": "",
                    "port": None,
                }
            }
        if action == "db_keys":
            if runtime_provider(account) != "agent_wechat":
                raise ValueError("db_keys is only available for runtime_provider=agent_wechat")
            from agent_wechat_runtime import AgentWechatManager

            return AgentWechatManager().export_db_keys(account)
        raise ValueError(f"unsupported action: {action}")


class ControlHandler(socketserver.StreamRequestHandler):
    registry: Registry

    def handle(self) -> None:
        raw = self.rfile.readline(1024 * 1024)
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            result = dispatch_action(self.registry, request)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "error": {"code": _error_code(exc), "message": str(exc)},
            }
        self.wfile.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


_UNIX_STREAM_SERVER = getattr(socketserver, "UnixStreamServer", None)


if _UNIX_STREAM_SERVER is not None:
    class ThreadingUnixServer(socketserver.ThreadingMixIn, _UNIX_STREAM_SERVER):
        daemon_threads = True
else:  # pragma: no cover - Windows development host; production is Linux.
    class ThreadingUnixServer:  # type: ignore[no-redef]
        pass


def create_server(socket_path: Path, registry: Registry) -> ThreadingUnixServer:
    if _UNIX_STREAM_SERVER is None:
        raise RuntimeErrorWithHint("Unix domain sockets are unavailable on this platform")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    handler = type("BoundRuntimeControlHandler", (ControlHandler,), {"registry": registry})
    server = ThreadingUnixServer(str(socket_path), handler)
    os.chmod(socket_path, 0o660)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path(os.environ.get("WECHAT_RUNTIME_CONTROL_SOCKET", DEFAULT_SOCKET)),
    )
    args = parser.parse_args(argv)
    registry = Registry(RuntimePaths.from_env())
    registry.load(create=False)
    server = create_server(args.socket, registry)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        args.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
