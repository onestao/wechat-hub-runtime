"""Regression tests for agent-wechat auth status = unknown handling in Runtime.

The patched agent-wechat upstream deliberately returns status=unknown when the
WeChat PID is alive but the current UI cannot be identified (for example
A11yUnavailable or Unidentified windows).  These tests pin the Runtime side of
that fresh-status contract:

* container running + agent server healthy + auth probe unknown
  -> runtime_health stays healthy and wechat_login_status is reported verbatim
     as "unknown"; it is never rewritten into logged_in.
* unknown observations are read-only: no stop/restart/remove/exec/container
  mutation may fire from the status or login-status path.
* the persisted agent-status.json must carry unknown (never a fresh
  logged_in marker derived from stale memory).
* the only designed unknown -> logged_in reconciliation is the explicit
  in-memory login-flow success marker; without it unknown stays unknown.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_module(name: str, path: Path):
    """Exec the module under its canonical name unless another test already did.

    test_wechat_runtime.py re-execs these same files when it loads later, which
    replaces the sys.modules entries.  Loading here is therefore conditional so
    this file never steals module identity from an already-loaded sibling, and
    every reference below goes through the object this helper returned.
    """

    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "root"
    / "scripts"
    / "wechat"
    / "wechat_runtime.py"
)
wechat_runtime = _load_module("wechat_runtime", MODULE_PATH)
agent_wechat_runtime = _load_module("agent_wechat_runtime", MODULE_PATH.with_name("agent_wechat_runtime.py"))
wechat_runtime_control = _load_module("wechat_runtime_control", MODULE_PATH.with_name("wechat_runtime_control.py"))


class ReadOnlyFakeEngine:
    """Docker engine double that records every mutating operation."""

    def __init__(self, containers=None):
        self.containers = containers or {}
        self.operations = []

    @property
    def available(self):
        return True

    def inspect_container(self, identifier):
        return self.containers.get(identifier)

    def managed_containers(self, account_id, *, provider="agent_wechat"):
        rows = []
        for container_id, value in self.containers.items():
            labels = (value.get("Config") or {}).get("Labels") or {}
            if (
                labels.get("com.wechat-hub.managed") == "true"
                and labels.get("com.wechat-hub.account-id") == account_id
                and labels.get("com.wechat-hub.provider") == provider
            ):
                rows.append({"Id": container_id})
        return rows

    def create_volume(self, name, device, labels):
        self.operations.append(("create_volume", name))

    def remove_volume(self, name):
        self.operations.append(("remove_volume", name))

    def create_container(self, name, payload):
        self.operations.append(("create_container", name))

    def start_container(self, identifier):
        self.operations.append(("start_container", identifier))

    def stop_container(self, identifier, timeout=10):
        self.operations.append(("stop_container", identifier))

    def remove_container(self, identifier, force=False):
        self.operations.append(("remove_container", identifier))

    def exec_container(self, identifier, command, **kwargs):
        self.operations.append(("exec_container", identifier))
        return 0, b""


class UnknownAuthStatusRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        policy = agent_wechat_runtime.AgentWechatManager._desired_primary_resource_policy(self.account)
        self.inspected = {
            "Id": "fake-alpha",
            "Name": "/wechat-agent-alpha",
            "Config": {
                "Image": "ghcr.io/onestao/wechat-hub-agent-wechat:0.11.15-wh.1",
                "Labels": agent_wechat_runtime._labels("alpha"),
            },
            "HostConfig": {
                "NetworkMode": "wechat-hub_default",
                "PidsLimit": policy["PidsLimit"],
                "Memory": policy["Memory"],
            },
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {}},
        }
        self.base = agent_wechat_runtime.AgentWechatManager._status_from_inspect(self.account, self.inspected)

    def make_manager(self, engine=None):
        return agent_wechat_runtime.AgentWechatManager(engine=engine if engine is not None else object())

    def test_healthy_agent_with_unknown_probe_reports_unknown_not_logged_in(self):
        manager = self.make_manager()
        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_probe_wechat_login", return_value=("unknown", "", "")
        ):
            status = manager._enrich_health(self.account, dict(self.base))
        self.assertTrue(status["container_running"])
        self.assertTrue(status["agent_server_healthy"])
        self.assertEqual(status["runtime_health"], "healthy")
        self.assertEqual(status["wechat_login_status"], "unknown")
        self.assertEqual(status["logged_in_user"], "")
        self.assertNotEqual(status["wechat_login_status"], "logged_in")

    def test_auth_probe_failure_falls_back_to_unknown(self):
        manager = self.make_manager()
        error = agent_wechat_runtime.AgentWechatRuntimeError("agent-wechat API returned HTTP 503: busy")
        with patch.object(manager, "_request_json_direct", side_effect=error):
            login_status, logged_in_user, login_error = manager._probe_wechat_login(self.account)
        self.assertEqual(login_status, "unknown")
        self.assertEqual(logged_in_user, "")
        self.assertIn("HTTP 503", login_error)

        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_request_json_direct", side_effect=error
        ):
            status = manager._enrich_health(self.account, dict(self.base))
        self.assertEqual(status["runtime_health"], "healthy")
        self.assertEqual(status["wechat_login_status"], "unknown")
        self.assertEqual(status["logged_in_user"], "")
        self.assertIn("HTTP 503", str(status.get("login_status_error") or ""))

    def test_login_status_unknown_is_never_success(self):
        manager = self.make_manager()
        with patch.object(
            manager,
            "status",
            return_value={
                "running": True,
                "container_running": True,
                "agent_server_healthy": True,
                "runtime_health": "healthy",
                "wechat_login_status": "unknown",
                "logged_in_user": "",
            },
        ):
            login = manager.login_status(self.account)
        self.assertEqual(login["auth_status"], "unknown")
        self.assertEqual(login["logged_in_user"], "")
        self.assertEqual(login["runtime_health"], "healthy")
        self.assertTrue(login["container_running"])
        self.assertFalse(login["snapshot_available"])
        self.assertNotIn("login_success", login)

    def test_unknown_status_is_read_only_and_persists_unknown(self):
        engine = ReadOnlyFakeEngine({"fake-alpha": self.inspected})
        manager = self.make_manager(engine)
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"WECHAT_RUNTIME_DIR": temp}, clear=False), patch.object(
                manager, "_probe_agent_server", return_value=(True, "")
            ), patch.object(manager, "_probe_wechat_login", return_value=("unknown", "", "")):
                status = manager.status(self.account)
                login = manager.login_status(self.account)
            self.assertEqual(status["wechat_login_status"], "unknown")
            self.assertEqual(login["auth_status"], "unknown")
            self.assertEqual(engine.operations, [])
            persisted = json.loads(
                (Path(temp) / "accounts" / "alpha" / "agent-status.json").read_text(encoding="utf-8")
            )
        self.assertEqual(persisted["wechat_login_status"], "unknown")
        self.assertEqual(persisted["logged_in_user"], "")
        self.assertNotEqual(persisted["wechat_login_status"], "logged_in")

    def test_unknown_without_login_flow_memory_does_not_finish_login(self):
        manager = self.make_manager()
        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": True,
            "state": "waiting_for_scan",
            "qr_data_url": "",
            "logged_in_user": "",
            "error": "",
            "status_message": "",
            "updated_at": 0.0,
        }
        with agent_wechat_runtime._LOGIN_FLOWS_LOCK:
            agent_wechat_runtime._LOGIN_FLOWS["alpha"] = flow
        try:
            with patch.object(
                manager,
                "status",
                return_value={
                    "running": True,
                    "container_running": True,
                    "agent_server_healthy": True,
                    "runtime_health": "healthy",
                    "wechat_login_status": "unknown",
                    "logged_in_user": "",
                },
            ):
                login = manager.login_status(self.account)
            self.assertEqual(login["auth_status"], "unknown")
            self.assertEqual(login["logged_in_user"], "")
            self.assertEqual(login["login_flow_state"], "waiting_for_scan")
        finally:
            agent_wechat_runtime._clear_login_flow("alpha")
        self.assertNotIn("alpha", agent_wechat_runtime._LOGIN_FLOWS)

    def test_completed_login_flow_marker_is_the_only_unknown_to_logged_in_bridge(self):
        # This reconciliation is the documented existing design: a finished
        # in-memory login flow may bridge a transient unknown probe back to
        # logged_in.  It must stay in-memory only and must not fire for a
        # waiting/running flow.
        manager = self.make_manager()
        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": False,
            "state": "logged_in",
            "qr_data_url": "",
            "logged_in_user": "wxid_flow",
            "error": "",
            "status_message": "",
            "updated_at": 0.0,
        }
        with agent_wechat_runtime._LOGIN_FLOWS_LOCK:
            agent_wechat_runtime._LOGIN_FLOWS["alpha"] = flow
        try:
            with patch.object(
                manager,
                "status",
                return_value={
                    "running": True,
                    "container_running": True,
                    "agent_server_healthy": True,
                    "runtime_health": "healthy",
                    "wechat_login_status": "unknown",
                    "logged_in_user": "",
                },
            ):
                login = manager.login_status(self.account)
            self.assertEqual(login["auth_status"], "logged_in")
            self.assertEqual(login["logged_in_user"], "wxid_flow")
        finally:
            agent_wechat_runtime._clear_login_flow("alpha")

    def test_degraded_list_status_falls_back_to_unknown(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        degraded = wechat_runtime_control._degraded_list_status(account, RuntimeError("probe timeout"))
        self.assertEqual(degraded["runtime_health"], "degraded")
        self.assertEqual(degraded["wechat_login_status"], "unknown")
        self.assertIsNone(degraded["running"])
        self.assertIsNone(degraded["container_running"])
        self.assertFalse(degraded["agent_server_healthy"])
        self.assertIn("probe timeout", degraded["health_error"])

    def test_control_login_status_dispatch_returns_unknown_verbatim(self):
        class SingleAccountRegistry:
            def load(self, create=False):
                del create
                return {
                    "accounts": [
                        {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
                    ]
                }

        login_payload = {
            "account_id": "alpha",
            "runtime_provider": "agent_wechat",
            "running": True,
            "container_running": True,
            "agent_server_healthy": True,
            "runtime_health": "healthy",
            "auth_status": "unknown",
            "logged_in_user": "",
            "login_flow_state": "idle",
        }
        # Patch the control module's own global lookup so this test does not
        # depend on which sibling test module owns the sys.modules identity of
        # agent_wechat_runtime.
        with patch.object(wechat_runtime_control, "login_status_for", return_value=login_payload):
            result = wechat_runtime_control.dispatch_action(
                SingleAccountRegistry(), {"action": "login_status", "account_id": "alpha"}
            )
        login = result["login"]
        self.assertEqual(login["auth_status"], "unknown")
        self.assertEqual(login["logged_in_user"], "")


if __name__ == "__main__":
    unittest.main()
