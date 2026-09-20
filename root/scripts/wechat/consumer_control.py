#!/usr/bin/env python3
"""Consumer Control: Disabled / EFB / Agent, with strict mutual exclusion.

The Console never touches the Docker socket.  The path is

    Console -> Core (authenticated management API) -> Runtime control plane -> Docker

Only the Runtime owns the Docker socket, so only the Runtime may create, start
or stop a consumer container.  EFB and Agent are **mutually exclusive**: a mode
change always stops the outgoing consumer and *confirms it stopped* before the
incoming one is started, so the two can never be RUNNING at the same time.

The Telegram token is never read into a response, never logged and never placed
in a URL.  The API only ever reports ``configured: true|false`` plus a
human-readable, token-free reason.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from agent_wechat_runtime import DockerEngine

logger = logging.getLogger("consumer_control")


class ConsumerControlError(RuntimeError):
    """Raised when a consumer operation cannot be completed safely."""

    def __init__(self, message: str, *, code: str = "consumer_operation_failed") -> None:
        super().__init__(message)
        self.code = code


CONSUMER_EFB = "efb"
CONSUMER_AGENT = "agent"
MODE_DISABLED = "disabled"
MODE_EFB = "efb"
MODE_AGENT = "agent"
MODES = (MODE_DISABLED, MODE_EFB, MODE_AGENT)

CONSUMER_LABEL = "com.wechat-hub.consumer"
CONSUMER_MANAGED_LABEL = "com.wechat-hub.consumer-managed"
CONSUMER_DISPLAY_LABEL = "com.wechat-hub.consumer-display"

STOP_CONFIRM_ATTEMPTS = 30
STOP_CONFIRM_INTERVAL_SEC = 0.5
START_CONFIRM_ATTEMPTS = 60
START_CONFIRM_INTERVAL_SEC = 0.5

CONSUMERS: dict[str, dict[str, Any]] = {
    CONSUMER_EFB: {
        "display_name": "EFB (Telegram)",
        "container_name": "wechat-hub-efb",
        "image_env": "EFB_IMAGE",
        "default_image": (
            "ghcr.io/onestao/wechat-hub-efb-linux-wechat-slave"
            "@sha256:620ff83d263e2c123e1e12052027a18e502f5f0545db285d0bcae510afa91bc9"
        ),
        "requires_configuration": True,
        "config_path_env": "EFB_PROFILE_DIR",
        "config_path_default": "/config/efb-profile",
        "mount_target": "/root/.ehforwarderbot",
        "summary": "把微信消息转发到 Telegram，并把 Telegram 回复发回微信",
    },
    CONSUMER_AGENT: {
        "display_name": "Agent",
        "container_name": "wechat-hub-agent",
        "image_env": "AGENT_IMAGE",
        "default_image": (
            "ghcr.io/onestao/wechat-hub-agent"
            "@sha256:0eff09ff197b5a27d2687cb5f4a11f23f55ea5c6e0a37cff00bb065beec35492"
        ),
        "requires_configuration": False,
        "config_path_env": "",
        "config_path_default": "",
        "mount_target": "/data",
        "summary": "内置消息消费者：在 Console 内直接处理消息，无需外部凭据",
    },
}

_STATE_LOCK = threading.RLock()
_TOKEN_LINE_RE = re.compile(r"^\s*token\s*:\s*\S+", re.MULTILINE)
_MASTER_RE = re.compile(r"^\s*master_channel\s*:\s*(\S+)", re.MULTILINE)


def _env_image(spec: dict[str, Any]) -> str:
    value = str(os.environ.get(str(spec["image_env"]) or "", "")).strip()
    return value or str(spec["default_image"])


def efb_configuration_state(profile_root: Path) -> tuple[bool, str]:
    """Whether EFB has a usable Telegram master channel.

    Never returns the token: only a boolean and a token-free reason.
    """

    config = profile_root / "profiles" / "default" / "config.yaml"
    if not config.is_file():
        return False, "尚未配置 Telegram：缺少 profiles/default/config.yaml"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "配置文件不可读"
    match = _MASTER_RE.search(text)
    if not match:
        return False, "尚未选择 master_channel"
    master = match.group(1).strip().strip("'\"")
    if not master:
        return False, "master_channel 为空"
    module_config = profile_root / "profiles" / "default" / master / "config.yaml"
    if not module_config.is_file():
        return False, f"{master} 尚未配置"
    try:
        module_text = module_config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, f"{master} 配置不可读"
    if not _TOKEN_LINE_RE.search(module_text):
        return False, f"{master} 尚未填写 token"
    return True, f"{master} 已配置"


class ConsumerControl:
    """Runtime-side supervisor for the optional message consumers."""

    def __init__(self, engine: DockerEngine | None = None) -> None:
        self.engine = engine or DockerEngine()

    # -- paths -------------------------------------------------------------

    def _host_config_root(self) -> str:
        """Host path behind this container's /config mount.

        Not a staticmethod: it needs the engine to inspect the Runtime itself.
        """
        inspected = self._self_inspect()
        for mount in inspected.get("Mounts") or []:
            if isinstance(mount, dict) and mount.get("Destination") == "/config":
                return str(mount.get("Source") or "")
        return ""

    def _self_inspect(self) -> dict[str, Any]:
        engine = self.engine
        identifier = os.environ.get("HOSTNAME", "").strip()
        inspected = engine.inspect_container(identifier) if identifier else None
        if inspected is None:
            raise ConsumerControlError("Docker Engine cannot inspect the Runtime container", code="runtime_unavailable")
        return inspected

    def _network(self) -> str:
        configured = os.environ.get("CONSUMER_NETWORK", "").strip() or os.environ.get(
            "AGENT_WECHAT_NETWORK", ""
        ).strip()
        if configured:
            return configured
        networks = (self._self_inspect().get("NetworkSettings") or {}).get("Networks") or {}
        if isinstance(networks, dict) and networks:
            return str(next(iter(networks.keys())))
        raise ConsumerControlError("Runtime is not attached to a Docker network", code="runtime_unavailable")

    @staticmethod
    def _desired_state_path() -> Path:
        root = Path(os.environ.get("WECHAT_RUNTIME_DIR", "/run/wechat-runtime"))
        return root / "consumers" / "desired-mode.json"

    def desired_mode(self) -> str:
        path = self._desired_state_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return MODE_DISABLED
        value = str((payload or {}).get("mode") or MODE_DISABLED)
        return value if value in MODES else MODE_DISABLED

    def _write_desired_mode(self, mode: str, *, detail: str = "") -> None:
        path = self._desired_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"mode": mode, "detail": detail, "updated_at": int(time.time())}
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    # -- observation -------------------------------------------------------

    def _find(self, consumer: str) -> dict[str, Any] | None:
        spec = CONSUMERS[consumer]
        name = str(spec["container_name"])
        inspected = self.engine.inspect_container(name)
        if inspected is None:
            return None
        labels = (inspected.get("Config") or {}).get("Labels") or {}
        if str(labels.get(CONSUMER_LABEL) or "") != consumer:
            # A container with the reserved name that this product does not own
            # must never be started or stopped by us.
            return None
        return inspected

    def _image_present(self, image: str) -> bool:
        try:
            self.engine.request(
                "GET", "/images/" + urllib.parse.quote(image, safe="") + "/json", expected=(200,)
            )
            return True
        except Exception:
            return False

    def _configuration(self, consumer: str) -> tuple[bool, str]:
        spec = CONSUMERS[consumer]
        if not spec["requires_configuration"]:
            return True, "无需额外配置"
        root = str(os.environ.get(str(spec["config_path_env"]) or "", "")).strip() or str(
            spec["config_path_default"]
        )
        return efb_configuration_state(Path(root))

    def _status(self, consumer: str) -> dict[str, Any]:
        spec = CONSUMERS[consumer]
        image = _env_image(spec)
        configured, configuration_detail = self._configuration(consumer)
        inspected = self._find(consumer) if self.engine.available else None
        running = bool(inspected and (inspected.get("State") or {}).get("Running"))
        state_obj = (inspected or {}).get("State") or {}
        exit_code = state_obj.get("ExitCode")
        if inspected is None:
            state = "not_provisioned"
        elif running:
            state = "running"
        elif isinstance(exit_code, int) and exit_code != 0:
            state = "failed"
        else:
            state = "stopped"
        current_image = str(((inspected or {}).get("Config") or {}).get("Image") or "")
        provisioned = inspected is not None
        image_present = provisioned or self._image_present(image)
        can_start = bool(configured and image_present)
        if not configured:
            blocked_reason = configuration_detail
        elif not image_present:
            blocked_reason = f"本机没有 {image}，请先拉取镜像"
        else:
            blocked_reason = ""
        return {
            "consumer": consumer,
            "display_name": str(spec["display_name"]),
            "summary": str(spec["summary"]),
            "container_name": str(spec["container_name"]),
            "container_id": str(((inspected or {}).get("Id") or "")),
            "image": image,
            "current_image": current_image,
            "image_present": image_present,
            "provisioned": provisioned,
            "configured": configured,
            "configuration_detail": configuration_detail,
            "state": state,
            "running": running,
            "started_at": str(state_obj.get("StartedAt") or ""),
            "finished_at": str(state_obj.get("FinishedAt") or ""),
            "restart_count": int(((inspected or {}).get("RestartCount") or 0)),
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "can_start": can_start,
            "blocked_reason": blocked_reason,
            "last_error": str(state_obj.get("Error") or ""),
        }

    def snapshot(self) -> dict[str, Any]:
        with _STATE_LOCK:
            statuses = {consumer: self._status(consumer) for consumer in CONSUMERS}
            running = [c for c, s in statuses.items() if s["running"]]
            current = running[0] if running else MODE_DISABLED
            return {
                "mode": current,
                "desired_mode": self.desired_mode(),
                "modes": list(MODES),
                "mutual_exclusion": True,
                "consumers": statuses,
                "runtime": {"available": bool(self.engine.available)},
            }

    # -- operations --------------------------------------------------------

    def _confirm_stopped(self, consumer: str) -> bool:
        for _ in range(STOP_CONFIRM_ATTEMPTS):
            inspected = self._find(consumer)
            if inspected is None or not bool((inspected.get("State") or {}).get("Running")):
                return True
            time.sleep(STOP_CONFIRM_INTERVAL_SEC)
        return False

    def stop(self, consumer: str) -> dict[str, Any]:
        if consumer not in CONSUMERS:
            raise ConsumerControlError(f"unknown consumer: {consumer}", code="invalid_request")
        with _STATE_LOCK:
            inspected = self._find(consumer)
            if inspected is not None and bool((inspected.get("State") or {}).get("Running")):
                identifier = str(inspected.get("Id") or CONSUMERS[consumer]["container_name"])
                self.engine.stop_container(identifier, timeout=10)
            stopped = self._confirm_stopped(consumer)
            if not stopped:
                raise ConsumerControlError(
                    f"{consumer} 未能在限定时间内停止；已中止切换以保证互斥",
                    code="consumer_stop_timeout",
                )
            return {"consumer": consumer, "stopped": True}

    def _create(self, consumer: str) -> dict[str, Any]:
        spec = CONSUMERS[consumer]
        name = str(spec["container_name"])
        image = _env_image(spec)
        host_root = self._host_config_root()
        if not host_root:
            raise ConsumerControlError("Runtime /config mount source is not visible through Docker inspect", code="runtime_unavailable")
        network = self._network()
        labels = {
            CONSUMER_LABEL: consumer,
            CONSUMER_MANAGED_LABEL: "true",
            CONSUMER_DISPLAY_LABEL: str(spec["display_name"]),
        }
        bind_source = f"{host_root}/{consumer}-profile" if consumer == CONSUMER_EFB else f"{host_root}/{consumer}-data"
        payload: dict[str, Any] = {
            "Image": image,
            "Labels": labels,
            "Env": [],
            "HostConfig": {
                "NetworkMode": network,
                "RestartPolicy": {"Name": "no"},
                "PidsLimit": 100,
                "Binds": [f"{bind_source}:{spec['mount_target']}"],
            },
        }
        if consumer == CONSUMER_AGENT:
            # ``NetworkMode`` = the shared internal network gives the container a
            # stable Docker-DNS name (``wechat-hub-agent``), so no address is
            # ever resolved, cached or exposed.  ``WECHAT_AGENT_DB`` is pinned to
            # the persistent /data bind: the Runtime recreates this container on
            # every mode switch, so the consumer's own state must not live in the
            # container filesystem.
            core_url = os.environ.get("CONSUMER_CORE_URL", "").strip() or "http://wechat-hub-core:8080"
            payload["Env"] = [
                f"WECHAT_CORE_URL={core_url}",
                f"WECHAT_AGENT_DB={spec['mount_target']}/agent.sqlite",
            ]
        return self.engine.create_container(name, payload)

    def _confirm_running(self, consumer: str) -> bool:
        for _ in range(START_CONFIRM_ATTEMPTS):
            inspected = self._find(consumer)
            if inspected is not None and bool((inspected.get("State") or {}).get("Running")):
                return True
            time.sleep(START_CONFIRM_INTERVAL_SEC)
        return False

    def start(self, consumer: str) -> dict[str, Any]:
        if consumer not in CONSUMERS:
            raise ConsumerControlError(f"unknown consumer: {consumer}")
        with _STATE_LOCK:
            status = self._status(consumer)
            if not status["configured"]:
                # Fail closed with an actionable message instead of a crash loop.
                raise ConsumerControlError(
                    f"{status['display_name']} 尚未配置：{status['configuration_detail']}",
                    code="consumer_not_configured",
                )
            if not status["image_present"]:
                raise ConsumerControlError(
                    f"{status['display_name']} 的镜像不可用：{status['image']}",
                    code="consumer_image_unavailable",
                )
            if not status["provisioned"]:
                self._create(consumer)
            inspected = self._find(consumer)
            if inspected is None:
                raise ConsumerControlError(f"{consumer} 容器创建后不可检查", code="consumer_create_failed")
            if not bool((inspected.get("State") or {}).get("Running")):
                self.engine.start_container(
                    str(inspected.get("Id") or CONSUMERS[consumer]["container_name"])
                )
            if not self._confirm_running(consumer):
                raise ConsumerControlError(f"{consumer} 启动后未进入 RUNNING 状态", code="consumer_start_failed")
            return {"consumer": consumer, "started": True}

    def set_mode(self, mode: str) -> dict[str, Any]:
        mode = str(mode or "").strip().lower()
        if mode not in MODES:
            raise ConsumerControlError(f"unsupported mode: {mode!r}", code="invalid_request")
        with _STATE_LOCK:
            snapshot = self.snapshot()
            running = [c for c, s in snapshot["consumers"].items() if s["running"]]
            # Mutual exclusion: stop everything that is not the requested mode
            # and *confirm* it stopped before starting the requested one.
            for consumer in running:
                if consumer != mode:
                    self.stop(consumer)
            if mode != MODE_DISABLED:
                self.start(mode)
            after = self.snapshot()
            still_running = [c for c, s in after["consumers"].items() if s["running"]]
            if len(still_running) > 1 or (mode == MODE_DISABLED and still_running):
                raise ConsumerControlError(
                    f"互斥校验失败：同时处于 RUNNING 的消费者为 {sorted(still_running)}",
                    code="consumer_mutual_exclusion_violation",
                )
            self._write_desired_mode(mode)
            after["desired_mode"] = mode
            return after
