#!/usr/bin/env python3
"""Thin Docker/API adapter for one-account-per-container agent-wechat runtimes.

WeChat Hub deliberately does not vendor agent-wechat sources here.  Runtime
owns Docker Engine access, creates one isolated upstream container per account,
and exposes only account-scoped lifecycle/status/login helpers to the existing
private Runtime control plane.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover - production image installs websocket-client
    websocket = None


DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_AGENT_WECHAT_IMAGE = "ghcr.io/thisnick/agent-wechat:0.11.15"
AGENT_WECHAT_PORT = 6174
DEFAULT_DESKTOP_GATEWAY_PORT = 17892
SELKIES_ATTACH_PORT = 8081
MANAGED_LABEL = "com.wechat-hub.managed"
ACCOUNT_LABEL = "com.wechat-hub.account-id"

logger = logging.getLogger("agent_wechat_runtime")
PROVIDER_LABEL = "com.wechat-hub.provider"
PROVIDER = "agent_wechat"
SELKIES_PROVIDER = "agent_wechat_selkies"
DESKTOP_PROVIDER_LABEL = "com.wechat-hub.desktop-provider"
PARENT_CONTAINER_LABEL = "com.wechat-hub.parent-container"

SELKIES_DESKTOP_FEATURES = {
    "mouse": True,
    "keyboard": True,
    "local_ime": True,
    "clipboard_text": False,
    "clipboard_image": False,
    "file_upload": True,
    # Uploads ride the WebSocket into the account browser-files volume, but
    # downloads need the nginx /files route that the raw attach companion
    # deliberately does not run. Advertise what is really reachable.
    "file_download": False,
    "dynamic_resize": True,
    "dpi_scaling": True,
}


def _selkies_clipboard_enabled() -> bool:
    """rc.2 safety policy: clipboard cannot be re-enabled by configuration.

    HTTPS is necessary for browser Clipboard APIs, but it does not make the
    upstream X11/xclip subprocess path safe.  Re-introducing clipboard requires
    a separately audited backend/reaper and a new release gate.
    """
    return False


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a safety-sensitive integer without allowing limits to be disabled."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if value <= 0:
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Read a safety-sensitive float and clamp it to a finite safe range."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not (value == value and value not in {float("inf"), float("-inf")}):
        value = default
    if value <= 0.0:
        value = default
    return max(minimum, min(maximum, value))


def selkies_desktop_features() -> dict[str, bool]:
    features = dict(SELKIES_DESKTOP_FEATURES)
    enabled = _selkies_clipboard_enabled()
    features["clipboard_text"] = enabled
    features["clipboard_image"] = enabled
    return features

NOVNC_DESKTOP_FEATURES = {
    "mouse": True,
    "keyboard": True,
    "local_ime": False,
    "clipboard_text": False,
    "clipboard_image": False,
    "file_upload": False,
    "file_download": False,
    "dynamic_resize": False,
    "dpi_scaling": False,
}


# The companion image is the Runtime image itself, but its s6 entrypoint is
# deliberately replaced with this fixed command.  It therefore starts neither
# Xvfb nor WeChat; it only attaches Selkies to the AgentWechat-owned Xvfb :99.
# The existing X server remains the one and only desktop/WeChat session for the
# account.
SELKIES_ATTACH_COMMAND = [
    "/bin/bash",
    "-c",
    """set -eu
export DISPLAY=:99
export HOME=/config
export XDG_CONFIG_HOME=/config/.config
export XDG_CACHE_HOME=/config/.cache
export XDG_DATA_HOME=/config/.local/share
export XDG_RUNTIME_DIR=/tmp/wechat-hub-selkies-runtime
mkdir -p "$HOME/Desktop" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
chmod 0777 "$HOME" "$HOME/Desktop"
i=0
while [ "$i" -lt 60 ]; do
  [ -S /tmp/.X11-unix/X99 ] && break
  i=$((i + 1))
  sleep 0.25
done
[ -S /tmp/.X11-unix/X99 ] || { echo 'selkies attach: X99 socket unavailable' >&2; exit 3; }
command -v selkies >/dev/null 2>&1 || { echo 'selkies attach: selkies executable unavailable' >&2; exit 4; }
env -u LD_PRELOAD selkies --addr=127.0.0.1 --port=8082 --mode=websockets --enable-https=false --enable-basic-auth=false --encoder=h264enc --enable-resize=true >/tmp/wechat-hub-selkies.log 2>&1 &
selkies_pid=$!
python3 /scripts/wechat/selkies_attach_gateway.py &
proxy_pid=$!
cleanup() {
  trap - EXIT INT TERM
  kill "$proxy_pid" 2>/dev/null || true
  kill "$selkies_pid" 2>/dev/null || true
  pkill -P "$selkies_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  wait "$selkies_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
set +e
wait -n "$selkies_pid" "$proxy_pid"
status=$?
set -e
cleanup
exit "$status"
""",
]


def _selkies_attach_env() -> list[str]:
    clipboard_val = "true|locked" if _selkies_clipboard_enabled() else "false|locked"
    return [
        "DISPLAY=:99",
        "HOME=/config",
        "USER=wechat",
        "LOGNAME=wechat",
        "LANG=zh_CN.UTF-8",
        "LC_ALL=zh_CN.UTF-8",
        "SELKIES_UI_TITLE=微信桌面",
        "SELKIES_UI_SHOW_LOGO=false|locked",
        "SELKIES_AUDIO_ENABLED=false|locked",
        "SELKIES_MICROPHONE_ENABLED=false|locked",
        "SELKIES_GAMEPAD_ENABLED=false|locked",
        f"SELKIES_CLIPBOARD_ENABLED={clipboard_val}",
        f"SELKIES_CLIPBOARD_IN_ENABLED={clipboard_val}",
        f"SELKIES_CLIPBOARD_OUT_ENABLED={clipboard_val}",
        f"SELKIES_ENABLE_BINARY_CLIPBOARD={clipboard_val}",
        "SELKIES_COMMAND_ENABLED=false|locked",
        "SELKIES_FILE_TRANSFERS=upload,download",
        "SELKIES_SECOND_SCREEN=false|locked",
        "SELKIES_ENABLE_SHARING=false|locked",
        "SELKIES_ENABLE_COLLAB=false|locked",
        "SELKIES_ENABLE_SHARED=false|locked",
        "SELKIES_ENABLE_PLAYER2=false|locked",
        "SELKIES_ENABLE_PLAYER3=false|locked",
        "SELKIES_ENABLE_PLAYER4=false|locked",
        "SELKIES_UI_SIDEBAR_SHOW_AUDIO_SETTINGS=false|locked",
        "SELKIES_UI_SIDEBAR_SHOW_APPS=false|locked",
        "SELKIES_UI_SIDEBAR_SHOW_SHARING=false|locked",
        "SELKIES_UI_SIDEBAR_SHOW_GAMEPADS=false|locked",
        "SELKIES_UI_SIDEBAR_SHOW_GAMING_MODE=false|locked",
        f"SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD={clipboard_val}",
        "SELKIES_UI_SIDEBAR_SHOW_FILES=true|locked",
        "SELKIES_UI_SIDEBAR_SHOW_SCREEN_SETTINGS=true|locked",
        "SELKIES_UI_SIDEBAR_SHOW_FULLSCREEN=true|locked",
        "SELKIES_UI_SIDEBAR_SHOW_TRACKPAD=true|locked",
        "SELKIES_UI_SIDEBAR_SHOW_KEYBOARD_BUTTON=true|locked",
        "SELKIES_SCALING_DPI=96,120,144,168,192,216,240,264,288",
    ]


SELKIES_ATTACH_ENV = _selkies_attach_env()

# Fixed, account-independent command template.  Console/API input is never
# interpolated into this shell program.  It only reconciles the upstream
# default X11 desktop (:99/5900) from view-only to interactive while retaining
# localhost-only VNC exposure.
INTERACTIVE_DESKTOP_COMMAND = [
    "/bin/sh",
    "-c",
    """set -eu
find_x11vnc() {
  ps -eo pid=,args= | awk '$0 ~ /[x]11vnc/ && $0 ~ /-display :99/ && $0 ~ /-rfbport 5900/ {print; exit}'
}
i=0
line=""
while [ "$i" -lt 30 ]; do
  line="$(find_x11vnc || true)"
  [ -n "$line" ] && break
  i=$((i + 1))
  sleep 0.5
done
[ -n "$line" ] || { echo 'state=missing'; exit 3; }
case " $line " in
  *" -viewonly "*) ;;
  *) echo 'state=interactive'; exit 0 ;;
esac
pid="$(printf '%s\n' "$line" | awk '{print $1}')"
case "$pid" in ''|*[!0-9]*) echo 'state=invalid-pid'; exit 4 ;; esac
kill "$pid"
i=0
while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 20 ]; do
  i=$((i + 1))
  sleep 0.1
done
if kill -0 "$pid" 2>/dev/null; then
  echo 'state=stop-timeout'
  exit 5
fi
nohup x11vnc -display :99 -forever -nopw -shared -xkb -rfbport 5900 -listen 127.0.0.1 >/tmp/wechat-hub-x11vnc.log 2>&1 &
i=0
while [ "$i" -lt 20 ]; do
  line="$(find_x11vnc || true)"
  if [ -n "$line" ]; then
    case " $line " in
      *" -viewonly "*) ;;
      *" -listen 127.0.0.1 "*) echo 'state=restarted'; exit 0 ;;
    esac
  fi
  i=$((i + 1))
  sleep 0.25
done
echo 'state=restart-failed'
exit 6
""",
]


_LOGIN_FLOWS: dict[str, dict[str, Any]] = {}
_LOGIN_FLOWS_LOCK = threading.Lock()


def _clear_login_flow(account_id: str) -> None:
    """Forget one account's ephemeral login state without touching persisted data."""

    with _LOGIN_FLOWS_LOCK:
        flow = _LOGIN_FLOWS.pop(account_id, None)
    if not flow:
        return
    lock = flow.get("lock")
    if lock is None:
        return
    with lock:
        flow["qr_data_url"] = ""
        flow["state"] = "discarded"


def _clear_desktop_sessions(account_id: str) -> None:
    """Revoke all opaque browser gateway sessions for one account."""

    root = Path(
        os.environ.get("WECHAT_DESKTOP_GATEWAY_SESSION_DIR", "/run/wechat-runtime/desktop-sessions")
    )
    if not root.is_dir():
        return
    for candidate in root.glob("*.json"):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and str(value.get("account_id") or "") == account_id:
            candidate.unlink(missing_ok=True)


class AgentWechatRuntimeError(RuntimeError):
    pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:  # pragma: no cover - Linux Docker host path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerEngine:
    """Small Docker Engine HTTP client; keeps docker.sock out of Core/Console."""

    def __init__(self, socket_path: str | None = None, *, timeout: float = 45.0) -> None:
        self.socket_path = socket_path or os.environ.get("DOCKER_HOST_SOCKET", DEFAULT_DOCKER_SOCKET)
        self.timeout = max(1.0, float(timeout))

    @property
    def available(self) -> bool:
        return os.name == "posix" and Path(self.socket_path).is_socket()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
        max_bytes: int = 32 * 1024 * 1024,
        timeout: float | None = None,
        binary: bool = False,
    ) -> Any:
        if not self.available:
            raise AgentWechatRuntimeError(
                f"Docker Engine socket is unavailable: {self.socket_path}; "
                "mount docker.sock only into the Runtime Manager"
            )
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request_timeout = self.timeout if timeout is None else max(1.0, float(timeout))
        conn = _UnixHTTPConnection(self.socket_path, request_timeout)
        try:
            conn.request(method.upper(), path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read(max_bytes + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise AgentWechatRuntimeError(f"Docker Engine request failed: {exc}") from exc
        finally:
            conn.close()
        if len(data) > max_bytes:
            raise AgentWechatRuntimeError("Docker Engine response exceeded safety limit")
        if binary:
            return data
        if response.status not in expected:
            detail = data.decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict) and parsed.get("message"):
                    detail = str(parsed["message"])
            except json.JSONDecodeError:
                pass
            raise AgentWechatRuntimeError(
                f"Docker Engine {method.upper()} {path} returned {response.status}: {detail or response.reason}"
            )
        if not data:
            return {}
        text = data.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def inspect_container(self, identifier: str) -> dict[str, Any] | None:
        path = "/containers/" + urllib.parse.quote(identifier, safe="") + "/json"
        try:
            value = self.request("GET", path, expected=(200,))
        except AgentWechatRuntimeError as exc:
            if " returned 404:" in str(exc):
                return None
            raise
        return value if isinstance(value, dict) else None

    def managed_containers(self, account_id: str, *, provider: str = PROVIDER) -> list[dict[str, Any]]:
        filters = json.dumps(
            {
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{ACCOUNT_LABEL}={account_id}",
                    f"{PROVIDER_LABEL}={provider}",
                ]
            },
            separators=(",", ":"),
        )
        query = urllib.parse.urlencode({"all": "1", "filters": filters})
        value = self.request("GET", f"/containers/json?{query}", expected=(200,))
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def create_volume(self, name: str, device: str, labels: dict[str, str]) -> None:
        payload = {
            "Name": name,
            "Driver": "local",
            "DriverOpts": {"type": "none", "o": "bind", "device": device},
            "Labels": labels,
        }
        self.request("POST", "/volumes/create", payload, expected=(201,))

    def remove_volume(self, name: str) -> None:
        path = "/volumes/" + urllib.parse.quote(name, safe="")
        try:
            self.request("DELETE", path, expected=(204,))
        except AgentWechatRuntimeError as exc:
            if " returned 404:" not in str(exc):
                raise

    def pull_image(self, image: str) -> None:
        repo, tag = _split_image_ref(image)
        query = urllib.parse.urlencode({"fromImage": repo, "tag": tag})
        try:
            pull_timeout = max(60.0, float(os.environ.get("AGENT_WECHAT_PULL_TIMEOUT", "900")))
        except ValueError:
            pull_timeout = 900.0
        self.request(
            "POST",
            f"/images/create?{query}",
            expected=(200,),
            max_bytes=64 * 1024 * 1024,
            timeout=pull_timeout,
        )

    def create_container(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode({"name": name})
        try:
            value = self.request("POST", f"/containers/create?{query}", payload, expected=(201,))
        except AgentWechatRuntimeError as exc:
            if "No such image" not in str(exc):
                raise
            self.pull_image(str(payload["Image"]))
            value = self.request("POST", f"/containers/create?{query}", payload, expected=(201,))
        return value if isinstance(value, dict) else {}

    def start_container(self, identifier: str) -> None:
        path = "/containers/" + urllib.parse.quote(identifier, safe="") + "/start"
        self.request("POST", path, expected=(204, 304))

    def stop_container(self, identifier: str, timeout: int = 10) -> None:
        path = "/containers/" + urllib.parse.quote(identifier, safe="") + f"/stop?t={max(1, int(timeout))}"
        self.request("POST", path, expected=(204, 304))

    def remove_container(self, identifier: str, *, force: bool = False) -> None:
        path = "/containers/" + urllib.parse.quote(identifier, safe="") + ("?force=1" if force else "")
        try:
            self.request("DELETE", path, expected=(204,))
        except AgentWechatRuntimeError as exc:
            if " returned 404:" not in str(exc):
                raise

    def exec_container(
        self,
        identifier: str,
        command: list[str],
        *,
        env: list[str] | None = None,
        timeout: float = 30.0,
        attach_stderr: bool = True,
    ) -> tuple[int, bytes]:
        """Run a bounded command in a child and return its stdout stream."""

        create_path = "/containers/" + urllib.parse.quote(identifier, safe="") + "/exec"
        created = self.request(
            "POST",
            create_path,
            {
                "AttachStdin": False,
                "AttachStdout": True,
                "AttachStderr": bool(attach_stderr),
                "Tty": False,
                "Env": env or [],
                "Cmd": command,
            },
            expected=(201,),
            timeout=timeout,
        )
        exec_id = str(created.get("Id") or "")
        if not exec_id:
            raise AgentWechatRuntimeError("Docker Engine did not return an exec id")

        encoded_exec_id = urllib.parse.quote(exec_id, safe="")
        output = self.request(
            "POST",
            f"/exec/{encoded_exec_id}/start",
            {"Detach": False, "Tty": False},
            expected=(200,),
            timeout=timeout,
            binary=True,
        )
        if not isinstance(output, bytes):
            raise AgentWechatRuntimeError("Docker Engine exec returned an invalid stream")
        state = self.request("GET", f"/exec/{encoded_exec_id}/json", expected=(200,), timeout=timeout)
        return int(state.get("ExitCode") or 0), _docker_stream_payload(output)


def _split_image_ref(image: str) -> tuple[str, str]:
    if "@" in image:
        # Digest-pinned refs are accepted as fromImage; tag is ignored by the
        # daemon for this form, but Docker's API still expects a tag parameter.
        return image, "latest"
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon], image[colon + 1 :] or "latest"
    return image, "latest"


def _docker_stream_payload(data: bytes) -> bytes:
    """Decode Docker's multiplexed stdout/stderr frames produced by exec."""

    if len(data) < 8:
        return data
    chunks: list[bytes] = []
    offset = 0
    framed = False
    while offset < len(data):
        if len(data) - offset < 8:
            break
        stream_type = data[offset]
        size = int.from_bytes(data[offset + 4 : offset + 8], "big")
        end = offset + 8 + size
        if stream_type not in {0, 1, 2} or end > len(data):
            break
        framed = True
        chunks.append(data[offset + 8 : end])
        offset = end
    return b"".join(chunks) if framed else data


def _labels(account_id: str) -> dict[str, str]:
    return {
        MANAGED_LABEL: "true",
        ACCOUNT_LABEL: account_id,
        PROVIDER_LABEL: PROVIDER,
    }


def _decode_data_url(value: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        raise AgentWechatRuntimeError("agent-wechat login endpoint did not return a PNG data URL")
    try:
        content = base64.b64decode(value[len(prefix) :], validate=True)
    except (ValueError, TypeError) as exc:
        raise AgentWechatRuntimeError("agent-wechat returned invalid QR PNG data") from exc
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AgentWechatRuntimeError("agent-wechat QR data is not PNG")
    return content


class AgentWechatManager:
    def __init__(self, engine: DockerEngine | None = None) -> None:
        self.engine = engine or DockerEngine()

    @staticmethod
    def image_for(account: dict[str, Any]) -> str:
        provider = account.get("agent_wechat") if isinstance(account.get("agent_wechat"), dict) else {}
        return str(provider.get("image") or os.environ.get("AGENT_WECHAT_IMAGE") or DEFAULT_AGENT_WECHAT_IMAGE).strip()

    @staticmethod
    def container_name(account: dict[str, Any]) -> str:
        from wechat_runtime import sanitize_account_runtime_name

        return f"wechat-agent-{sanitize_account_runtime_name(str(account['id']))}"

    @staticmethod
    def desktop_container_name(account: dict[str, Any]) -> str:
        from wechat_runtime import sanitize_account_runtime_name

        return f"wechat-desktop-{sanitize_account_runtime_name(str(account['id']))}"

    @staticmethod
    def storage_names(account: dict[str, Any]) -> tuple[str, str]:
        safe = AgentWechatManager.container_name(account).removeprefix("wechat-agent-")
        return f"wechat-agent-{safe}-data", f"wechat-agent-{safe}-home"

    @staticmethod
    def desktop_storage_names(account: dict[str, Any]) -> tuple[str, str]:
        safe = AgentWechatManager.container_name(account).removeprefix("wechat-agent-")
        return f"wechat-agent-{safe}-x11", f"wechat-agent-{safe}-browser-files"

    @staticmethod
    def runtime_storage_root(account: dict[str, Any]) -> Path:
        safe = AgentWechatManager.container_name(account).removeprefix("wechat-agent-")
        return Path("/config/agent-wechat") / safe

    def prepare_files(self, account: dict[str, Any]) -> dict[str, str]:
        root = self.runtime_storage_root(account)
        data = root / "data"
        home = root / "home"
        x11 = root / "x11"
        browser_files = root / "browser-files"
        token = root / "auth-token"
        desktop_token = root / "desktop-auth-token"
        data.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        x11.mkdir(parents=True, exist_ok=True)
        browser_files.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(x11, 0o1777)
            # The upload/download bridge is account-private at the Docker
            # volume boundary.  Both the Selkies companion and upstream's
            # non-root WeChat process need to create/read files in it.
            os.chmod(browser_files, 0o777)
        except OSError:
            pass
        if not token.exists():
            token.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        if not desktop_token.exists():
            desktop_token.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        try:
            os.chmod(token, 0o600)
            os.chmod(desktop_token, 0o600)
        except OSError:
            pass
        return {
            "root": str(root),
            "data": str(data),
            "home": str(home),
            "x11": str(x11),
            "browser_files": str(browser_files),
            "token": str(token),
            "desktop_token": str(desktop_token),
        }

    def _self_inspect(self) -> dict[str, Any]:
        identifier = os.environ.get("HOSTNAME", "").strip()
        if not identifier:
            try:
                identifier = Path("/etc/hostname").read_text(encoding="utf-8").strip()
            except OSError:
                identifier = ""
        if not identifier:
            raise AgentWechatRuntimeError("cannot determine Runtime Manager container id")
        inspected = self.engine.inspect_container(identifier)
        if inspected is None:
            raise AgentWechatRuntimeError("Docker Engine cannot inspect the Runtime Manager container")
        return inspected

    def _host_config_root_and_network(self) -> tuple[str, str]:
        inspected = self._self_inspect()
        source = ""
        for mount in inspected.get("Mounts") or []:
            if isinstance(mount, dict) and mount.get("Destination") == "/config":
                source = str(mount.get("Source") or "")
                break
        if not source:
            raise AgentWechatRuntimeError("Runtime /config mount source is not visible through Docker inspect")
        configured = os.environ.get("AGENT_WECHAT_NETWORK", "").strip()
        networks = inspected.get("NetworkSettings", {}).get("Networks", {})
        if configured:
            network = configured
        elif isinstance(networks, dict) and networks:
            network = next(iter(networks.keys()))
        else:
            raise AgentWechatRuntimeError("Runtime Manager is not attached to a Docker network")
        return source, network

    def _ensure_volumes(self, account: dict[str, Any], host_config_root: str) -> tuple[str, str, str]:
        files = self.prepare_files(account)
        data_volume, home_volume = self.storage_names(account)
        labels = _labels(str(account["id"]))
        rel_root = Path(files["root"]).relative_to("/config")
        host_root = Path(host_config_root) / rel_root
        self.engine.create_volume(data_volume, str(host_root / "data"), labels)
        self.engine.create_volume(home_volume, str(host_root / "home"), labels)
        host_token = str(host_root / "auth-token")
        return data_volume, home_volume, host_token

    def _ensure_desktop_volumes(self, account: dict[str, Any], host_config_root: str) -> tuple[str, str]:
        files = self.prepare_files(account)
        x11_volume, browser_files_volume = self.desktop_storage_names(account)
        labels = _labels(str(account["id"]))
        labels[DESKTOP_PROVIDER_LABEL] = "selkies"
        rel_root = Path(files["root"]).relative_to("/config")
        host_root = Path(host_config_root) / rel_root
        self.engine.create_volume(x11_volume, str(host_root / "x11"), labels)
        self.engine.create_volume(browser_files_volume, str(host_root / "browser-files"), labels)
        return x11_volume, browser_files_volume

    def _reset_x11_socket_dir(self, account: dict[str, Any]) -> None:
        """Clear only ephemeral X11 socket artifacts while the primary is stopped."""

        root = Path(self.prepare_files(account)["x11"])
        for child in root.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError as exc:
                raise AgentWechatRuntimeError(f"cannot reset account X11 socket directory: {exc}") from exc
        try:
            os.chmod(root, 0o1777)
        except OSError:
            pass

    def _desktop_token_host_path(self, account: dict[str, Any], host_config_root: str) -> str:
        files = self.prepare_files(account)
        rel_root = Path(files["root"]).relative_to("/config")
        return str(Path(host_config_root) / rel_root / "desktop-auth-token")

    def _find_container(self, account: dict[str, Any]) -> dict[str, Any] | None:
        matches = self.engine.managed_containers(str(account["id"]))
        if len(matches) > 1:
            raise AgentWechatRuntimeError(
                f"multiple managed agent-wechat containers found for account {account['id']}; refusing ambiguous control"
            )
        if not matches:
            return None
        identifier = str(matches[0].get("Id") or "")
        return self.engine.inspect_container(identifier) if identifier else None

    def _find_desktop_container(self, account: dict[str, Any]) -> dict[str, Any] | None:
        matches = self.engine.managed_containers(str(account["id"]), provider=SELKIES_PROVIDER)
        if len(matches) > 1:
            raise AgentWechatRuntimeError(
                f"multiple managed Selkies desktop containers found for account {account['id']}; refusing ambiguous control"
            )
        if not matches:
            return None
        identifier = str(matches[0].get("Id") or "")
        return self.engine.inspect_container(identifier) if identifier else None

    @staticmethod
    def _mount_targets(inspected: dict[str, Any]) -> set[str]:
        targets: set[str] = set()
        for mount in inspected.get("Mounts") or []:
            if isinstance(mount, dict):
                target = str(mount.get("Destination") or mount.get("Target") or "")
                if target:
                    targets.add(target)
        for mount in (inspected.get("HostConfig") or {}).get("Mounts") or []:
            if isinstance(mount, dict):
                target = str(mount.get("Target") or mount.get("Destination") or "")
                if target:
                    targets.add(target)
        return targets

    @classmethod
    def _selkies_attach_mounts_ready(cls, inspected: dict[str, Any]) -> bool:
        targets = cls._mount_targets(inspected)
        return "/tmp/.X11-unix" in targets and "/home/wechat/WeChatHubFiles" in targets

    @staticmethod
    def _desktop_labels(account: dict[str, Any], parent_container_id: str) -> dict[str, str]:
        return {
            MANAGED_LABEL: "true",
            ACCOUNT_LABEL: str(account["id"]),
            PROVIDER_LABEL: SELKIES_PROVIDER,
            DESKTOP_PROVIDER_LABEL: "selkies",
            PARENT_CONTAINER_LABEL: parent_container_id,
        }

    def selkies_image_for(self) -> str:
        configured = os.environ.get("WECHAT_SELKIES_ATTACH_IMAGE", "").strip()
        if configured:
            return configured
        inspected = self._self_inspect()
        # Prefer the immutable image ID of the running Runtime Manager.  The
        # companion therefore reuses already-present layers and cannot drift
        # from the Selkies build that WeChat Hub itself was tested with.
        image = str(inspected.get("Image") or "").strip()
        if not image:
            image = str((inspected.get("Config") or {}).get("Image") or "").strip()
        if not image:
            raise AgentWechatRuntimeError("cannot determine Runtime image for Selkies desktop companion")
        return image

    def _runtime_dri_devices(self) -> list[dict[str, str]]:
        """Reuse only the Runtime Manager's explicitly configured /dev/dri devices."""

        try:
            inspected = self._self_inspect()
        except Exception:
            # GPU forwarding is an optional optimization; inability to inspect
            # it must never prevent the safer CPU encoder fallback.
            return []
        devices: list[dict[str, str]] = []
        for item in (inspected.get("HostConfig") or {}).get("Devices") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("PathOnHost") or "")
            target = str(item.get("PathInContainer") or "")
            if not host.startswith("/dev/dri") or not target.startswith("/dev/dri"):
                continue
            devices.append(
                {
                    "PathOnHost": host,
                    "PathInContainer": target,
                    "CgroupPermissions": str(item.get("CgroupPermissions") or "rwm"),
                }
            )
        return devices

    def _selkies_payload(
        self,
        account: dict[str, Any],
        *,
        parent_container_id: str,
        x11_volume: str,
        browser_files_volume: str,
        host_desktop_token: str,
    ) -> dict[str, Any]:
        labels = self._desktop_labels(account, parent_container_id)
        env = list(_selkies_attach_env())
        env.append(f"WECHAT_ACCOUNT_ID={account['id']}")
        pids_limit = _bounded_int_env(
            "WECHAT_SELKIES_PIDS_LIMIT", 100, minimum=32, maximum=512
        )
        mem_limit_mb = _bounded_int_env(
            "WECHAT_SELKIES_MEM_LIMIT_MB", 1024, minimum=256, maximum=4096
        )
        cpu_cores = _bounded_float_env(
            "WECHAT_SELKIES_CPU_LIMIT_CORES", 2.0, minimum=0.25, maximum=4.0
        )

        host_config: dict[str, Any] = {
            "Mounts": [
                {"Type": "volume", "Source": x11_volume, "Target": "/tmp/.X11-unix"},
                {"Type": "volume", "Source": browser_files_volume, "Target": "/config"},
                {
                    "Type": "bind",
                    "Source": host_desktop_token,
                    "Target": "/run/secrets/wechat-hub-desktop-token",
                    "ReadOnly": True,
                },
            ],
            # X11 MIT-SHM must refer to the same IPC namespace as the
            # Xvfb owner. Linux abstract X11 sockets are scoped to the
            # network namespace as well, so share only this account's
            # primary network namespace. PID/files remain isolated.
            "IpcMode": f"container:{parent_container_id}",
            "NetworkMode": f"container:{parent_container_id}",
            # Docker's tiny init remains PID 1 and reaps orphaned/zombie
            # helpers while forwarding termination to the Bash lifecycle
            # supervisor below.
            "Init": True,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "PidsLimit": pids_limit,
            "Memory": mem_limit_mb * 1024 * 1024,
            "NanoCpus": int(cpu_cores * 1e9),
        }
        devices = self._runtime_dri_devices()
        if devices:
            host_config["Devices"] = devices
        return {
            "Image": self.selkies_image_for(),
            "Entrypoint": SELKIES_ATTACH_COMMAND[:2],
            "Cmd": [SELKIES_ATTACH_COMMAND[2]],
            "User": "0:0",
            "WorkingDir": "/config",
            "Env": env,
            "Labels": labels,
            "HostConfig": host_config,
        }

    def _probe_selkies(self, account: dict[str, Any], *, timeout: float = 2.0) -> tuple[bool, str]:
        # The companion shares the primary AgentWechat network namespace and
        # is therefore reached through the existing account-scoped DNS name.
        url = f"http://{self.container_name(account)}:{SELKIES_ATTACH_PORT}/"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"X-WeChat-Hub-Desktop-Token": self._desktop_token(account)},
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.25, timeout)) as response:
                response.read(64 * 1024)
                status = int(getattr(response, "status", 200))
                if 200 <= status < 400 or status == 426:
                    return True, ""
                return False, f"Selkies desktop returned HTTP {status}"
        except urllib.error.HTTPError as exc:
            if exc.code == 426:
                return True, ""
            return False, f"Selkies desktop returned HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            return False, f"Selkies desktop health failed: {exc}"

    def _remove_selkies_container(self, account: dict[str, Any]) -> None:
        if not self.engine.available:
            return
        inspected = self._find_desktop_container(account)
        if inspected is None:
            return
        identifier = str(inspected.get("Id") or self.desktop_container_name(account))
        if bool((inspected.get("State") or {}).get("Running")):
            self.engine.stop_container(identifier, timeout=5)
        self.engine.remove_container(identifier, force=False)

    def ensure_selkies_desktop(self, account: dict[str, Any]) -> dict[str, Any]:
        """Start an on-demand Selkies companion attached to the existing Xvfb.

        No WeChat process and no X server is started here.  The primary
        AgentWechat child remains the sole owner of both.  Older live children
        that predate the shared X11/files mounts are intentionally not
        recreated under the user's feet; callers may fall back to noVNC until
        the operator performs a normal account restart.
        """

        primary = self._find_container(account)
        if primary is None or not bool((primary.get("State") or {}).get("Running")):
            raise AgentWechatRuntimeError("AgentWechat container is not running")
        parent_id = self._validate_managed_container(account, primary)
        self._assert_no_running_resource_drift(account, primary)
        if not self._selkies_attach_mounts_ready(primary):
            raise AgentWechatRuntimeError(
                "Selkies desktop requires one normal account restart to attach the shared X11/files volumes"
            )

        host_config_root, _network = self._host_config_root_and_network()
        x11_volume, browser_files_volume = self._ensure_desktop_volumes(account, host_config_root)
        host_desktop_token = self._desktop_token_host_path(account, host_config_root)
        desired_image = self.selkies_image_for()
        existing = self._find_desktop_container(account)
        if existing is not None:
            labels = (existing.get("Config") or {}).get("Labels") or {}
            current_image = str((existing.get("Config") or {}).get("Image") or "")
            host = existing.get("HostConfig") or {}
            stale = (
                str(labels.get(PARENT_CONTAINER_LABEL) or "") != parent_id
                or current_image != desired_image
                or str(host.get("IpcMode") or "") != f"container:{parent_id}"
                or str(host.get("NetworkMode") or "") != f"container:{parent_id}"
            )
            if stale:
                self._remove_selkies_container(account)
                existing = None

        try:
            if existing is None:
                payload = self._selkies_payload(
                    account,
                    parent_container_id=parent_id,
                    x11_volume=x11_volume,
                    browser_files_volume=browser_files_volume,
                    host_desktop_token=host_desktop_token,
                )
                created = self.engine.create_container(self.desktop_container_name(account), payload)
                identifier = str(created.get("Id") or self.desktop_container_name(account))
                existing = self.engine.inspect_container(identifier)
            if existing is None:
                raise AgentWechatRuntimeError("Selkies desktop creation did not produce an inspectable container")

            identifier = str(existing.get("Id") or self.desktop_container_name(account))
            if not bool((existing.get("State") or {}).get("Running")):
                self.engine.start_container(identifier)

            deadline = time.monotonic() + 15.0
            last_error = "Selkies desktop did not become ready"
            while time.monotonic() < deadline:
                healthy, last_error = self._probe_selkies(account, timeout=1.0)
                if healthy:
                    return {
                        "account_id": str(account["id"]),
                        "desktop_provider": "selkies",
                        "container_name": self.desktop_container_name(account),
                        "port": SELKIES_ATTACH_PORT,
                        "display": ":99",
                        "features": dict(selkies_desktop_features()),
                        "browser_files_path": "/home/wechat/WeChatHubFiles/Desktop",
                    }
                time.sleep(0.25)
            # A broken companion must not sit around consuming resources.  The
            # caller may safely fall back to noVNC without touching WeChat.
            self._remove_selkies_container(account)
            raise AgentWechatRuntimeError(last_error)
        except Exception:
            self._remove_selkies_container(account)
            raise

    @classmethod
    def _desired_primary_resource_policy(
        cls, account: dict[str, Any] | None = None
    ) -> dict[str, int]:
        del account
        return {
            "PidsLimit": _bounded_int_env(
                "AGENT_WECHAT_PIDS_LIMIT", 512, minimum=64, maximum=1024
            ),
            "Memory": _bounded_int_env(
                "AGENT_WECHAT_MEM_LIMIT_MB", 2048, minimum=512, maximum=8192
            )
            * 1024
            * 1024,
        }

    @classmethod
    def _primary_resource_policy_drift(
        cls, inspected: dict[str, Any] | None, account: dict[str, Any] | None = None
    ) -> dict[str, dict[str, int]]:
        if not inspected:
            return {}
        host_config = inspected.get("HostConfig") or {}
        if not isinstance(host_config, dict):
            return {}
        desired = cls._desired_primary_resource_policy(account)
        drift: dict[str, dict[str, int]] = {}
        for key, desired_value in desired.items():
            current_value = host_config.get(key)
            if current_value != desired_value:
                drift[key] = {"current": current_value, "desired": desired_value}
        return drift

    def _container_payload(
        self,
        account: dict[str, Any],
        *,
        network: str,
        data_volume: str,
        home_volume: str,
        host_token: str,
        x11_volume: str = "",
        browser_files_volume: str = "",
    ) -> dict[str, Any]:
        name = self.container_name(account)
        try:
            shm_size = max(64, int(os.environ.get("AGENT_WECHAT_SHM_MB", "512"))) * 1024 * 1024
        except ValueError:
            shm_size = 512 * 1024 * 1024
        labels = _labels(str(account["id"]))
        labels["com.wechat-hub.image"] = self.image_for(account)
        mounts = [
            {"Type": "volume", "Source": data_volume, "Target": "/data"},
            {"Type": "volume", "Source": home_volume, "Target": "/home/wechat"},
            {"Type": "bind", "Source": host_token, "Target": "/data/auth-token", "ReadOnly": True},
        ]
        if x11_volume:
            mounts.append({"Type": "volume", "Source": x11_volume, "Target": "/tmp/.X11-unix"})
        if browser_files_volume:
            mounts.append(
                {
                    "Type": "volume",
                    "Source": browser_files_volume,
                    "Target": "/home/wechat/WeChatHubFiles",
                }
            )
        return {
            "Image": self.image_for(account),
            "Labels": labels,
            "ExposedPorts": {f"{AGENT_WECHAT_PORT}/tcp": {}},
            "HostConfig": {
                "Mounts": mounts,
                "CapAdd": ["SYS_PTRACE"],
                "SecurityOpt": ["seccomp=unconfined"],
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                # Port 6174 is deliberately NOT published to the Docker host.
                # Runtime and Desktop Gateway reach this account only through
                # the shared internal WeChat Hub Docker network.
                "NetworkMode": network,
                "IpcMode": "shareable",
                "ShmSize": shm_size,
                "PidsLimit": self._desired_primary_resource_policy(account)["PidsLimit"],
                "Memory": self._desired_primary_resource_policy(account)["Memory"],
            },
            "NetworkingConfig": {"EndpointsConfig": {network: {"Aliases": [name]}}},
        }

    def ensure_container(self, account: dict[str, Any]) -> dict[str, Any]:
        host_config_root, network = self._host_config_root_and_network()
        data_volume, home_volume, host_token = self._ensure_volumes(account, host_config_root)
        x11_volume, browser_files_volume = self._ensure_desktop_volumes(account, host_config_root)
        existing = self._find_container(account)
        desired_image = self.image_for(account)
        if existing is not None:
            current_image = str(existing.get("Config", {}).get("Image") or "")
            running = bool(existing.get("State", {}).get("Running"))
            needs_desktop_mounts = not self._selkies_attach_mounts_ready(existing)
            resource_drift = self._primary_resource_policy_drift(existing, account)
            if not running:
                self._reset_x11_socket_dir(account)
            if (current_image != desired_image or needs_desktop_mounts or bool(resource_drift)) and not running:
                self.engine.remove_container(str(existing.get("Id") or self.container_name(account)), force=False)
                existing = None
            elif running and resource_drift:
                logger.warning(
                    "agent-wechat container %s for account %s has resource policy drift: %s; "
                    "refusing inline recreation while running, awaiting controlled stop/reconcile",
                    existing.get("Id") or self.container_name(account),
                    account.get("id"),
                    resource_drift,
                )
        else:
            self._reset_x11_socket_dir(account)
        if existing is None:
            payload = self._container_payload(
                account,
                network=network,
                data_volume=data_volume,
                home_volume=home_volume,
                host_token=host_token,
                x11_volume=x11_volume,
                browser_files_volume=browser_files_volume,
            )
            created = self.engine.create_container(self.container_name(account), payload)
            identifier = str(created.get("Id") or self.container_name(account))
            existing = self.engine.inspect_container(identifier)
        if existing is None:
            raise AgentWechatRuntimeError("agent-wechat container creation did not produce an inspectable container")
        return existing

    @classmethod
    def _status_from_inspect(cls, account: dict[str, Any], inspected: dict[str, Any] | None) -> dict[str, Any]:
        desired = cls.image_for(account)
        base = {
            "account_id": account["id"],
            "display_name": str(account.get("display_name") or account["id"]),
            "runtime_provider": PROVIDER,
            "enabled": bool(account.get("enabled", True)),
            "autostart": bool(account.get("autostart", True)),
            "legacy": False,
            "username": account.get("username"),
            "uid": None,
            "home": account.get("home"),
            "display": "isolated",
            "pids": [],
            "windows": [],
            "window_error": None,
            "container_name": cls.container_name(account),
            "container_id": "",
            "image": desired,
            "current_image": "",
            "running": False,
            "container_running": False,
            "agent_server_healthy": None,
            "runtime_health": "stopped",
            "health_error": "",
            "wechat_login_status": "stopped",
            "logged_in_user": "",
            "capabilities": {
                "send_text": True,
                "send_image": True,
                "send_file": True,
                "desktop": True,
                "native": False,
            },
            "resource_policy_drift": {},
            "resource_reconcile_required": False,
        }
        if inspected is None:
            return base
        container_id = str(inspected.get("Id") or "")
        base["container_id"] = container_id
        base["container_name"] = str(inspected.get("Name") or "").lstrip("/") or base["container_name"]
        base["current_image"] = str(inspected.get("Config", {}).get("Image") or "")
        base["running"] = bool(inspected.get("State", {}).get("Running"))
        base["container_running"] = base["running"]
        drift = cls._primary_resource_policy_drift(inspected, account)
        base["resource_policy_drift"] = drift
        base["resource_reconcile_required"] = bool(base["running"] and drift)
        return base

    def _internal_url(self, account: dict[str, Any], path: str) -> str:
        return f"http://{self.container_name(account)}:{AGENT_WECHAT_PORT}{path}"

    def _request_json_direct(
        self,
        account: dict[str, Any],
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 20.0,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        body = None
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token(account)}"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._internal_url(account, path), data=body, headers=headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(64 * 1024).decode("utf-8", errors="replace").strip()
            raise AgentWechatRuntimeError(f"agent-wechat API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            raise AgentWechatRuntimeError(f"agent-wechat API request failed: {exc}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise AgentWechatRuntimeError("agent-wechat API response is too large")
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentWechatRuntimeError("agent-wechat API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AgentWechatRuntimeError("agent-wechat API response must be an object")
        return value

    def _probe_agent_server(self, account: dict[str, Any], *, timeout: float = 3.0) -> tuple[bool, str]:
        """Probe the unauthenticated upstream /health endpoint on the internal network."""

        request = urllib.request.Request(self._internal_url(account, "/health"), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=max(0.5, float(timeout))) as response:
                response.read(64 * 1024)
                if 200 <= int(getattr(response, "status", 200)) < 300:
                    return True, ""
                return False, f"agent-wechat /health returned HTTP {getattr(response, 'status', 'unknown')}"
        except urllib.error.HTTPError as exc:
            return False, f"agent-wechat /health returned HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            return False, f"agent-wechat /health failed: {exc}"

    def _probe_wechat_login(self, account: dict[str, Any], *, timeout: float = 5.0) -> tuple[str, str, str]:
        try:
            auth = self._request_json_direct(
                account, "GET", "/api/status/auth", timeout=max(0.5, float(timeout)), authenticated=True
            )
        except AgentWechatRuntimeError as exc:
            return "unknown", "", str(exc)
        return (
            str(auth.get("status") or "unknown"),
            str(auth.get("loggedInUser") or auth.get("logged_in_user") or ""),
            "",
        )

    def _enrich_health(
        self,
        account: dict[str, Any],
        status: dict[str, Any],
        *,
        probe_timeout: float | None = None,
    ) -> dict[str, Any]:
        container_running = bool(status.get("container_running") or status.get("running"))
        status["container_running"] = container_running
        if not container_running:
            status["agent_server_healthy"] = None
            status["runtime_health"] = "stopped"
            status["health_error"] = ""
            status["wechat_login_status"] = "stopped"
            status["logged_in_user"] = ""
            return status

        drift_error = self._running_resource_drift_error(account)
        if drift_error:
            status["agent_server_healthy"] = None
            status["runtime_health"] = "degraded"
            status["health_error"] = drift_error
            status["wechat_login_status"] = "quarantined-resource-drift"
            status["logged_in_user"] = ""
            status["resource_reconcile_required"] = True
            return status

        health_timeout = 3.0 if probe_timeout is None else max(0.5, float(probe_timeout))
        login_timeout = 5.0 if probe_timeout is None else max(0.5, float(probe_timeout))
        healthy, health_error = self._probe_agent_server(account, timeout=health_timeout)
        status["agent_server_healthy"] = healthy
        status["health_error"] = health_error
        if not healthy:
            status["runtime_health"] = "degraded"
            status["wechat_login_status"] = "unknown"
            status["logged_in_user"] = ""
            return status

        login_status, logged_in_user, login_error = self._probe_wechat_login(account, timeout=login_timeout)
        status["runtime_health"] = "healthy"
        status["wechat_login_status"] = login_status
        status["logged_in_user"] = logged_in_user
        if login_error:
            status["login_status_error"] = login_error
        else:
            status.pop("login_status_error", None)
        return status

    @staticmethod
    def _persist_status(account: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        runtime_root = Path(os.environ.get("WECHAT_RUNTIME_DIR", "/run/wechat-runtime"))
        target_dir = runtime_root / "accounts" / str(account["id"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "agent-status.json"
        temp = target.with_suffix(".json.tmp")
        payload = dict(status)
        payload["updated_at"] = int(time.time())
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
        return status

    def status(self, account: dict[str, Any], *, probe_timeout: float | None = None) -> dict[str, Any]:
        if not self.engine.available:
            result = self._status_from_inspect(account, None)
            result["window_error"] = f"Docker Engine socket unavailable: {self.engine.socket_path}"
            return result
        result = self._status_from_inspect(account, self._find_container(account))
        return self._persist_status(
            account,
            self._enrich_health(account, result, probe_timeout=probe_timeout),
        )

    @staticmethod
    def _validate_managed_container(account: dict[str, Any], inspected: dict[str, Any]) -> str:
        labels = (inspected.get("Config") or {}).get("Labels") or {}
        expected_account_id = str(account["id"])
        if not isinstance(labels, dict) or (
            str(labels.get(MANAGED_LABEL) or "") != "true"
            or str(labels.get(ACCOUNT_LABEL) or "") != expected_account_id
            or str(labels.get(PROVIDER_LABEL) or "") != PROVIDER
        ):
            raise AgentWechatRuntimeError(
                f"refusing managed container operation: container labels do not match managed account {expected_account_id}"
            )
        identifier = str(inspected.get("Id") or "")
        if not identifier:
            raise AgentWechatRuntimeError("refusing managed container operation: managed container has no id")
        return identifier

    def _running_resource_drift_error(
        self, account: dict[str, Any], inspected: dict[str, Any] | None = None
    ) -> str:
        if inspected is None:
            engine = self.engine
            if not hasattr(engine, "managed_containers") or not hasattr(engine, "inspect_container"):
                return ""
            inspected = self._find_container(account)
        if not inspected or not bool((inspected.get("State") or {}).get("Running")):
            return ""
        drift = self._primary_resource_policy_drift(inspected, account)
        if not drift:
            return ""
        return (
            "agent-wechat container is quarantined due to running resource policy drift "
            f"({drift}); controlled restart required"
        )

    def _assert_no_running_resource_drift(
        self, account: dict[str, Any], inspected: dict[str, Any] | None = None
    ) -> None:
        error = self._running_resource_drift_error(account, inspected)
        if error:
            raise AgentWechatRuntimeError(error)

    def ensure_interactive_desktop(self, account: dict[str, Any]) -> dict[str, Any]:
        """Reconcile upstream's default x11vnc into safe interactive mode.

        This intentionally uses Docker Engine Exec rather than a custom
        agent-wechat image.  The command is fixed in this module and only
        targets DISPLAY=:99 / rfbport=5900.  No VNC port is published to the
        host; browser traffic still traverses upstream websockify/agent-server
        and the WeChat Hub Desktop Gateway.
        """

        if not self.engine.available:
            raise AgentWechatRuntimeError("Docker Engine is unavailable for desktop reconciliation")
        inspected = self._find_container(account)
        if inspected is None:
            raise AgentWechatRuntimeError("agent-wechat desktop container does not exist")
        identifier = self._validate_managed_container(account, inspected)
        if not bool((inspected.get("State") or {}).get("Running")):
            raise AgentWechatRuntimeError("agent-wechat desktop container is stopped")
        self._assert_no_running_resource_drift(account, inspected)
        exit_code, output = self.engine.exec_container(
            identifier,
            list(INTERACTIVE_DESKTOP_COMMAND),
            timeout=25.0,
            attach_stderr=True,
        )
        text = output.decode("utf-8", errors="replace").strip()
        if exit_code != 0:
            raise AgentWechatRuntimeError(
                f"interactive desktop reconciliation failed ({exit_code}): {text or 'unknown error'}"
            )
        state = "interactive" if "state=interactive" in text else "restarted" if "state=restarted" in text else "unknown"
        if state == "unknown":
            raise AgentWechatRuntimeError("interactive desktop reconciliation returned an unknown state")
        return {
            "account_id": str(account["id"]),
            "display": ":99",
            "rfbport": 5900,
            "listen": "127.0.0.1",
            "interactive": True,
            "action": state,
        }

    def start(self, account: dict[str, Any]) -> dict[str, Any]:
        inspected = self.ensure_container(account)
        identifier = str(inspected.get("Id") or self.container_name(account))
        if not bool(inspected.get("State", {}).get("Running")):
            self.engine.start_container(identifier)
        refreshed = self.engine.inspect_container(identifier)
        is_running = bool((refreshed.get("State") or {}).get("Running")) if refreshed else False
        drift = self._primary_resource_policy_drift(refreshed, account) if refreshed else {}
        desktop = None
        if is_running and not drift:
            desktop = self.ensure_interactive_desktop(account)
        result = self._enrich_health(account, self._status_from_inspect(account, refreshed))
        if desktop:
            result["interactive_desktop"] = desktop
        if is_running and drift:
            logger.warning(
                "agent-wechat container %s for account %s has resource policy drift: %s; "
                "skipping interactive desktop exec to avoid EAGAIN under pressure, controlled restart required",
                identifier,
                account.get("id"),
                drift,
            )
            result["action"] = "running-resource-drift"
            result["resource_reconcile_required"] = True
        else:
            result["action"] = "started" if result["running"] else "launch-dispatched"
        if result["current_image"] and result["current_image"] != result["image"]:
            result["image_update_pending"] = True
        return self._persist_status(account, result)

    def stop(self, account: dict[str, Any]) -> dict[str, Any]:
        _clear_login_flow(str(account["id"]))
        _clear_desktop_sessions(str(account["id"]))
        self._remove_selkies_container(account)
        inspected = self._find_container(account)
        if inspected is None:
            result = self._status_from_inspect(account, None)
            result["action"] = "already-stopped"
            return self._persist_status(account, result)
        identifier = str(inspected.get("Id") or self.container_name(account))
        if bool(inspected.get("State", {}).get("Running")):
            self.engine.stop_container(identifier)
        result = self._enrich_health(
            account, self._status_from_inspect(account, self.engine.inspect_container(identifier))
        )
        result["action"] = "stopped"
        return self._persist_status(account, result)

    def restart(self, account: dict[str, Any]) -> dict[str, Any]:
        self.stop(account)
        result = self.start(account)
        result["action"] = "restarted"
        return result

    def remove(self, account: dict[str, Any], *, purge_data: bool = False) -> dict[str, Any]:
        _clear_login_flow(str(account["id"]))
        _clear_desktop_sessions(str(account["id"]))
        self._remove_selkies_container(account)
        inspected = self._find_container(account) if self.engine.available else None
        if inspected is not None:
            identifier = str(inspected.get("Id") or self.container_name(account))
            if bool(inspected.get("State", {}).get("Running")):
                self.engine.stop_container(identifier)
            self.engine.remove_container(identifier, force=False)
        data_volume, home_volume = self.storage_names(account)
        x11_volume, browser_files_volume = self.desktop_storage_names(account)
        root = self.runtime_storage_root(account)
        if purge_data:
            if self.engine.available:
                self.engine.remove_volume(data_volume)
                self.engine.remove_volume(home_volume)
                self.engine.remove_volume(x11_volume)
                self.engine.remove_volume(browser_files_volume)
            if root.is_dir() and str(root).startswith("/config/agent-wechat/"):
                shutil.rmtree(root)
        return {
            "removed": account["id"],
            "runtime_provider": PROVIDER,
            "preserve_data": not purge_data,
            "data_volume": data_volume,
            "home_volume": home_volume,
            "x11_volume": x11_volume,
            "browser_files_volume": browser_files_volume,
            "data_path": str(root),
        }

    def _token(self, account: dict[str, Any]) -> str:
        token = self.prepare_files(account)["token"]
        value = Path(token).read_text(encoding="utf-8").strip()
        if not value:
            raise AgentWechatRuntimeError("agent-wechat token file is empty")
        return value

    def _desktop_token(self, account: dict[str, Any]) -> str:
        token = self.prepare_files(account)["desktop_token"]
        value = Path(token).read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Fa-f0-9]{64}", value):
            raise AgentWechatRuntimeError("Selkies desktop token is invalid")
        return value

    def api_request(
        self,
        account: dict[str, Any],
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        status = self.status(account)
        if not status.get("container_running"):
            raise AgentWechatRuntimeError("agent-wechat container is stopped")
        if status.get("agent_server_healthy") is not True:
            raise AgentWechatRuntimeError(
                str(status.get("health_error") or "agent-wechat agent-server health check failed")
            )
        return self._request_json_direct(account, method, path, payload, timeout=timeout, authenticated=True)

    @staticmethod
    def _login_flow_snapshot(account_id: str) -> dict[str, Any]:
        with _LOGIN_FLOWS_LOCK:
            flow = _LOGIN_FLOWS.get(account_id)
            if not flow:
                return {}
            lock = flow.get("lock")
        if lock is None:
            return {}
        with lock:
            return {
                key: value
                for key, value in flow.items()
                if key not in {"lock", "thread"}
            }

    def _run_login_flow(self, account: dict[str, Any], flow: dict[str, Any]) -> None:
        lock = flow["lock"]
        try:
            if websocket is None:
                raise AgentWechatRuntimeError(
                    "websocket-client is unavailable in Runtime; rebuild the Runtime image"
                )
            token = self._token(account)
            try:
                timeout_ms = max(
                    30_000,
                    min(900_000, int(os.environ.get("AGENT_WECHAT_LOGIN_TIMEOUT_MS", "300000"))),
                )
            except ValueError:
                timeout_ms = 300_000
            query = urllib.parse.urlencode(
                {
                    "timeoutMs": str(timeout_ms),
                    "newAccount": "false",
                    "token": token,
                }
            )
            url = f"ws://{self.container_name(account)}:{AGENT_WECHAT_PORT}/api/ws/login?{query}"
            ws = websocket.create_connection(
                url,
                timeout=max(10.0, timeout_ms / 1000.0 + 15.0),
                enable_multithread=True,
            )
            try:
                with lock:
                    flow["state"] = "authenticating"
                    flow["error"] = ""
                while True:
                    raw = ws.recv()
                    if raw in (None, "", b""):
                        raise AgentWechatRuntimeError(
                            "agent-wechat login WebSocket closed before login_success"
                        )
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        event = json.loads(str(raw))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    now = time.time()
                    with lock:
                        flow["updated_at"] = now
                        event_type = str(event.get("type") or "").strip().lower().replace("-", "_")
                        message = event.get("message")
                        if isinstance(message, str) and message.strip():
                            flow["status_message"] = message.strip()
                        qr = event.get("qr") if isinstance(event.get("qr"), dict) else {}
                        data_url = str(
                            qr.get("qrDataUrl")
                            or qr.get("qr_data_url")
                            or event.get("qrDataUrl")
                            or event.get("qrData")
                            or event.get("qr_data_url")
                            or ""
                        )
                        if data_url:
                            # QR bytes are intentionally process-memory only.
                            flow["qr_data_url"] = data_url
                            flow["state"] = "waiting_for_scan"
                        status_event = event.get("status")
                        if isinstance(status_event, dict):
                            flow["status_message"] = str(status_event.get("message") or "")
                        if "phone_confirm" in event or event_type == "phone_confirm":
                            flow["state"] = "phone_confirm"
                            flow["qr_data_url"] = ""
                        success = event.get("login_success")
                        if isinstance(success, dict) or event_type == "login_success":
                            details = success if isinstance(success, dict) else event
                            flow["state"] = "logged_in"
                            flow["logged_in_user"] = str(
                                details.get("userId") or details.get("user_id") or ""
                            )
                            flow["qr_data_url"] = ""
                            break
                        if "login_timeout" in event or event_type == "login_timeout":
                            flow["state"] = "timeout"
                            flow["qr_data_url"] = ""
                            break
                        error = event.get("error")
                        if isinstance(error, dict) or event_type == "error":
                            error_message = ""
                            if isinstance(error, dict):
                                error_message = str(error.get("message") or "")
                            elif isinstance(error, str):
                                error_message = error
                            flow["state"] = "error"
                            flow["error"] = error_message or str(message or "agent-wechat login flow failed")
                            flow["qr_data_url"] = ""
                            break
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        except Exception as exc:
            with lock:
                flow["state"] = "error"
                flow["error"] = str(exc)
        finally:
            with lock:
                flow["running"] = False
                flow["updated_at"] = time.time()

    def _ensure_login_flow(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = str(account["id"])
        with _LOGIN_FLOWS_LOCK:
            flow = _LOGIN_FLOWS.get(account_id)
            replace = not flow
            if flow:
                lock = flow["lock"]
                with lock:
                    terminal = str(flow.get("state") or "") in {"timeout", "error"}
                    replace = terminal and not bool(flow.get("running"))
            if replace:
                flow = {
                    "lock": threading.Lock(),
                    "thread": None,
                    "running": False,
                    "state": "starting",
                    "qr_data_url": "",
                    "logged_in_user": "",
                    "error": "",
                    "status_message": "",
                    "updated_at": time.time(),
                }
                _LOGIN_FLOWS[account_id] = flow
            assert flow is not None
            lock = flow["lock"]
            with lock:
                if not flow.get("running") and flow.get("state") != "logged_in":
                    thread = threading.Thread(
                        target=self._run_login_flow,
                        args=(dict(account), flow),
                        name=f"agent-wechat-login-{account_id}",
                        daemon=True,
                    )
                    flow["thread"] = thread
                    flow["running"] = True
                    thread.start()
        return flow

    def login_status(self, account: dict[str, Any]) -> dict[str, Any]:
        status = self.status(account)
        auth_status = str(status.get("wechat_login_status") or "unknown")
        logged_in_user = str(status.get("logged_in_user") or "")
        flow = self._login_flow_snapshot(str(account["id"]))
        flow_state = str(flow.get("state") or "")
        if flow and flow_state != "logged_in" and bool(flow.get("running")) and auth_status == "logged_in":
            # The visible chat UI can appear before the full upstream login
            # plan finishes detecting the account and persisting DB credentials.
            # Do not report success to Console until Login WebSocket emits
            # login_success, otherwise Sync can race the credential extraction.
            auth_status = "unknown"
            logged_in_user = ""
        if (
            flow_state == "logged_in"
            and status.get("agent_server_healthy") is True
            and auth_status in {"unknown", "app_not_running"}
        ):
            auth_status = "logged_in"
            logged_in_user = str(flow.get("logged_in_user") or logged_in_user)
        return {
            "account_id": account["id"],
            "display_name": str(account.get("display_name") or account["id"]),
            "runtime_provider": PROVIDER,
            "running": bool(status.get("container_running")),
            "container_running": bool(status.get("container_running")),
            "agent_server_healthy": status.get("agent_server_healthy"),
            "runtime_health": str(status.get("runtime_health") or "unknown"),
            "pids": [],
            "windows": [],
            "snapshot_available": bool(
                status.get("container_running")
                and status.get("agent_server_healthy") is True
                and auth_status != "logged_in"
                and flow.get("qr_data_url")
            ),
            "window_id": None,
            "window_title": "agent-wechat",
            "auth_status": auth_status,
            "logged_in_user": logged_in_user,
            "login_flow_state": flow_state or "idle",
            "login_flow_status": str(flow.get("status_message") or ""),
            "login_flow_error": str(flow.get("error") or ""),
        }

    def start_login(self, account: dict[str, Any]) -> dict[str, Any]:
        """Start or reuse one full upstream login FSM without waiting for QR bytes."""

        status = self.status(account)
        if not status.get("container_running"):
            raise AgentWechatRuntimeError("agent-wechat container is stopped")
        if status.get("agent_server_healthy") is not True:
            raise AgentWechatRuntimeError(
                str(status.get("health_error") or "agent-wechat agent-server is unhealthy")
            )
        account_id = str(account["id"])
        if str(status.get("wechat_login_status") or "") == "logged_in":
            return {
                "account_id": account_id,
                "runtime_provider": PROVIDER,
                "running": True,
                "snapshot_available": False,
                "login_flow_state": "logged_in",
                "login_flow_status": "",
                "login_flow_error": "",
            }

        existing = self._login_flow_snapshot(account_id)
        if existing.get("state") == "logged_in" and not existing.get("running"):
            # A stale in-memory success marker must not prevent a fresh login
            # after the upstream auth probe reports logged_out.
            _clear_login_flow(account_id)
        self._ensure_login_flow(account)
        flow = self._login_flow_snapshot(account_id)
        return {
            "account_id": account_id,
            "runtime_provider": PROVIDER,
            "running": True,
            "snapshot_available": bool(flow.get("qr_data_url")),
            "login_flow_state": str(flow.get("state") or "starting"),
            "login_flow_status": str(flow.get("status_message") or ""),
            "login_flow_error": str(flow.get("error") or ""),
        }

    def capture_login(self, account: dict[str, Any]) -> dict[str, Any]:
        status = self.status(account)
        if not status.get("container_running"):
            raise AgentWechatRuntimeError("agent-wechat container is stopped")
        if status.get("agent_server_healthy") is not True:
            raise AgentWechatRuntimeError(
                str(status.get("health_error") or "agent-wechat agent-server is unhealthy")
            )
        flow = self._login_flow_snapshot(str(account["id"]))
        data_url = str(flow.get("qr_data_url") or "")
        if not data_url:
            return {
                "account_id": account["id"],
                "runtime_provider": PROVIDER,
                "status": "qr_not_ready",
                "login_flow_state": str(flow.get("state") or "idle"),
                "login_flow_status": str(flow.get("status_message") or ""),
                "login_flow_error": str(flow.get("error") or ""),
            }
        content = _decode_data_url(data_url)
        if len(content) > 1400 * 1024:
            raise AgentWechatRuntimeError("agent-wechat QR PNG is unexpectedly large")
        return {
            "account_id": account["id"],
            "content_type": "image/png",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "window_id": None,
            "runtime_provider": PROVIDER,
        }

    def desktop(self, account: dict[str, Any], *, desktop_provider: str = "auto") -> dict[str, Any]:
        status = self.status(account)
        if not status.get("container_running"):
            raise AgentWechatRuntimeError("agent-wechat desktop is not available while the container is stopped")
        if status.get("agent_server_healthy") is not True:
            raise AgentWechatRuntimeError(
                str(status.get("health_error") or "agent-wechat desktop upstream is unhealthy")
            )
        self._assert_no_running_resource_drift(account)
        requested_provider = str(desktop_provider or "auto").strip().lower().replace("-", "_")
        if requested_provider not in {"auto", "selkies", "novnc", "no_vnc"}:
            raise AgentWechatRuntimeError("desktop_provider must be auto, selkies, or novnc")

        selected_provider = "novnc"
        features = dict(NOVNC_DESKTOP_FEATURES)
        fallback_reason = ""
        if requested_provider in {"auto", "selkies"} and os.environ.get(
            "WECHAT_SELKIES_ATTACH_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}:
            try:
                selkies = self.ensure_selkies_desktop(account)
                selected_provider = "selkies"
                features = dict(selkies.get("features") or selkies_desktop_features())
            except AgentWechatRuntimeError as exc:
                if requested_provider == "selkies":
                    raise
                fallback_reason = str(exc)

        if selected_provider == "novnc":
            self.ensure_interactive_desktop(account)
        session_id = secrets.token_urlsafe(32)
        try:
            ttl = max(60, min(86_400, int(os.environ.get("WECHAT_DESKTOP_GATEWAY_SESSION_TTL", "14400"))))
        except ValueError:
            ttl = 14_400
        session_dir = Path(
            os.environ.get("WECHAT_DESKTOP_GATEWAY_SESSION_DIR", "/run/wechat-runtime/desktop-sessions")
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        # Opportunistically remove expired descriptors. They contain no
        # upstream token, but should not accumulate indefinitely.
        for candidate in session_dir.glob("*.json"):
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
                if int(value.get("expires_at") or 0) < now:
                    candidate.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        descriptor = {
            "account_id": str(account["id"]),
            "runtime_provider": PROVIDER,
            "desktop_provider": selected_provider,
            "created_at": now,
            "expires_at": now + ttl,
        }
        target = session_dir / f"{session_id}.json"
        target.write_text(json.dumps(descriptor, separators=(",", ":")) + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        try:
            gateway_port = int(os.environ.get("WECHAT_DESKTOP_GATEWAY_PORT", str(DEFAULT_DESKTOP_GATEWAY_PORT)))
        except ValueError:
            gateway_port = DEFAULT_DESKTOP_GATEWAY_PORT
        public_scheme = os.environ.get("WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME", "http").strip().lower()
        if public_scheme not in {"http", "https"}:
            public_scheme = "http"
        public_host = os.environ.get("WECHAT_DESKTOP_GATEWAY_PUBLIC_HOST", "").strip()
        public_port_raw = os.environ.get("WECHAT_DESKTOP_GATEWAY_PUBLIC_PORT", "").strip()
        if public_port_raw:
            try:
                public_port = int(public_port_raw)
            except ValueError:
                public_port = gateway_port
            if not 1 <= public_port <= 65535:
                public_port = gateway_port
        else:
            public_port = gateway_port
        if selected_provider == "selkies":
            path = f"/desktop/{session_id}/"
        else:
            # The Gateway is a pure path-prefix proxy, so the advertised
            # browser path carries upstream's /vnc/websockify route below the
            # opaque session id. Upstream has no /websockify route at root.
            websocket_path = urllib.parse.quote(f"desktop/{session_id}/vnc/websockify", safe="")
            path = f"/desktop/{session_id}/vnc/?autoconnect=true&path={websocket_path}"
        result = {
            "account_id": account["id"],
            "runtime_provider": PROVIDER,
            "desktop_provider": selected_provider,
            "scheme": public_scheme,
            "host": public_host,
            "port": public_port,
            "path": path,
            "gateway_session_expires_at": now + ttl,
            "cache_control": "no-store",
            "features": features,
        }
        if selected_provider == "selkies":
            result["file_exchange_path"] = "/home/wechat/WeChatHubFiles/Desktop"
        if fallback_reason:
            result["fallback_reason"] = fallback_reason
        return result

    def export_db_keys(self, account: dict[str, Any]) -> dict[str, Any]:
        """Read upstream's stored, verified DB credentials for this account.

        This is intentionally implemented in the Runtime Driver so Core does
        not need to know where upstream stores its internal SQLite state.  The
        result crosses only the private Runtime Unix control socket.
        """

        state_db = self.runtime_storage_root(account) / "data" / "agent.db"
        if not state_db.is_file():
            raise AgentWechatRuntimeError(f"agent-wechat state DB is unavailable: {state_db}")
        container = self._find_container(account)
        if not container or not bool((container.get("State") or {}).get("Running")):
            raise AgentWechatRuntimeError("agent-wechat state DB requires a running upstream container")
        identifier = self._validate_managed_container(account, container)
        self._assert_no_running_resource_drift(account, container)

        token = self._token(account)
        if not re.fullmatch(r"[A-Fa-f0-9]{32,256}", token):
            raise AgentWechatRuntimeError("agent-wechat token is not in the expected format")
        # agent-wechat 0.11.15 uses the API token as an SQLCipher passphrase.
        # The secret only ever travels through the exec environment, so the
        # heredoc delimiter must stay unquoted for $AGENT_TOKEN to expand.
        command = [
            "/bin/sh",
            "-c",
            'sqlcipher "$AGENT_DB_PATH" <<AGENT_SQL\n'
            "PRAGMA key = \"$AGENT_TOKEN\";\n"
            ".mode json\n"
            "SELECT account_dir, db_name, hex_key, COALESCE(verified_at, '') AS verified_at "
            "FROM wechat_keys ORDER BY COALESCE(verified_at, '') DESC;\n"
            "AGENT_SQL\n",
        ]
        exit_code, output = self.engine.exec_container(
            identifier,
            command,
            env=[f"AGENT_TOKEN={token}"],
            timeout=30.0,
            attach_stderr=False,
        )
        if exit_code != 0:
            raise AgentWechatRuntimeError("cannot read agent-wechat stored DB credentials")
        text = output.decode("utf-8", errors="replace").strip()
        # PRAGMA key answers with a single "ok" row before JSON mode is enabled.
        if text.startswith("ok\n"):
            text = text[3:].strip()
        if not text:
            raise AgentWechatRuntimeError("agent-wechat has no stored DB credentials yet")
        try:
            rows = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentWechatRuntimeError("agent-wechat returned invalid credential metadata") from None
        if not isinstance(rows, list):
            raise AgentWechatRuntimeError("agent-wechat returned invalid credential metadata")
        credentials = []
        for row in rows[:2048]:
            if not isinstance(row, dict):
                continue
            credentials.append(
                {
                    "account_dir": str(row.get("account_dir") or ""),
                    "db_name": str(row.get("db_name") or ""),
                    "hex_key": str(row.get("hex_key") or ""),
                    "verified_at": str(row.get("verified_at") or ""),
                }
            )
        return {
            "account_id": account["id"],
            "runtime_provider": PROVIDER,
            "credentials": credentials,
        }
