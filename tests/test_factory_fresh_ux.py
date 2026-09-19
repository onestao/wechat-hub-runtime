"""Regression tests for the Factory Fresh UX remediation.

Covers the three P0 defects found by the first real user of a Factory Fresh
deployment:

* P0-1 — "interactive desktop reconciliation failed (143)" was a *false*
  failure.  The reconciliation shell program matched its own command line in
  ``ps`` output, read its own PID and SIGTERM'd itself.  The operation result
  must never outrank the observed Docker/runtime post-condition.
* P0-2 — a login that really succeeded could still be reported as failed
  because the Login WebSocket witness never delivered ``login_success``.
* P0-3 — the WeChat self profile (nickname / wechat_id / avatar) has to reach
  the identity projection instead of the wxid being shown as a nickname.
"""

import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_module(name: str, path: Path):
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
agent_wechat_runtime = _load_module(
    "agent_wechat_runtime", MODULE_PATH.with_name("agent_wechat_runtime.py")
)
wechat_runtime_control = _load_module(
    "wechat_runtime_control", MODULE_PATH.with_name("wechat_runtime_control.py")
)


ACCOUNT = {
    "id": "alpha",
    "runtime_alias": "alpha",
    "display_name": "Alpha",
    "resource_key": "alpha-11d4b2d9",
    "instance_uuid": "11111111-2222-3333-4444-555555555555",
    "runtime_provider": "agent_wechat",
}
IMAGE = agent_wechat_runtime.AgentWechatManager.image_for(ACCOUNT)
NETWORK = "wechat-hub-internal"


def inspected_container(*, running=True, image=IMAGE, network=NETWORK):
    host_config = dict(
        agent_wechat_runtime.AgentWechatManager._desired_primary_resource_policy(ACCOUNT)
    )
    return {
        "Id": "fake-alpha",
        "Config": {"Image": image, "Labels": agent_wechat_runtime._labels(ACCOUNT)},
        "State": {"Running": running},
        "NetworkSettings": {"Networks": {network: {}}} if network else {"Networks": {}},
        "HostConfig": host_config,
        "Mounts": [],
    }


class FakeEngine:
    """Docker engine double that records mutating operations."""

    def __init__(self, containers=None):
        self.containers = containers or {}
        self.operations = []
        self.exec_queue = []

    @property
    def available(self):
        return True

    def inspect_container(self, identifier):
        return self.containers.get(identifier)

    def managed_containers(self, *args, **kwargs):
        return [{"Id": key} for key in self.containers]

    def exec_container(self, identifier, command, **kwargs):
        self.operations.append(("exec", identifier))
        if self.exec_queue:
            return self.exec_queue.pop(0)
        return 0, b"state=interactive\n"


class DesktopReconciliationTests(unittest.TestCase):
    """P0-1 — operation result AND observed post-condition."""

    def test_program_cannot_match_itself(self):
        script = agent_wechat_runtime.INTERACTIVE_DESKTOP_COMMAND[-1]
        # The matcher must key on the real executable name and must exclude the
        # shell itself; otherwise the program reads its own PID and SIGTERMs
        # itself (exit 143 with empty output).
        self.assertIn("comm=", script)
        self.assertIn("$1 == self", script)
        self.assertIn("self=$$", script)
        self.assertIn("state=self-match", script)

    def make_manager(self, engine):
        return agent_wechat_runtime.AgentWechatManager(engine=engine)

    def test_reconcile_returns_true_state_when_post_condition_holds(self):
        engine = FakeEngine({"fake-alpha": inspected_container()})
        engine.exec_queue = [(143, b""), (143, b""), (143, b"")]
        manager = self.make_manager(engine)
        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_expected_network", return_value=NETWORK
        ), patch.object(agent_wechat_runtime.time, "sleep", return_value=None):
            desktop = manager.ensure_interactive_desktop(dict(ACCOUNT))

        self.assertEqual(desktop["action"], "reconciled")
        self.assertIsNone(desktop["interactive"])
        self.assertIn("143", desktop["reconciliation"]["operation_error"])
        self.assertTrue(all(desktop["reconciliation"]["post_condition"].values()))

    def test_reconcile_fails_closed_when_post_condition_missing(self):
        engine = FakeEngine({"fake-alpha": inspected_container(running=False)})
        engine.exec_queue = [(143, b""), (143, b""), (143, b"")]
        manager = self.make_manager(engine)
        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_expected_network", return_value=NETWORK
        ), patch.object(agent_wechat_runtime.time, "sleep", return_value=None):
            with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
                manager.ensure_interactive_desktop(dict(ACCOUNT))

    def test_reconcile_requires_exact_image(self):
        engine = FakeEngine(
            {"fake-alpha": inspected_container(image="ghcr.io/onestao/wechat-hub-agent-wechat:latest")}
        )
        engine.exec_queue = [(143, b""), (143, b""), (143, b"")]
        manager = self.make_manager(engine)
        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_expected_network", return_value=NETWORK
        ), patch.object(agent_wechat_runtime.time, "sleep", return_value=None):
            with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
                manager.ensure_interactive_desktop(dict(ACCOUNT))

    def test_post_condition_rejects_foreign_container(self):
        foreign = inspected_container()
        foreign["Config"]["Labels"] = {"com.wechat-hub.managed": "true"}
        engine = FakeEngine({"fake-alpha": foreign})
        manager = self.make_manager(engine)
        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_expected_network", return_value=NETWORK
        ):
            satisfied, evidence = manager.desktop_post_condition(dict(ACCOUNT))
        self.assertFalse(satisfied)
        self.assertFalse(evidence["ownership_ok"])

    def _conflict_engine(self, existing):
        engine = agent_wechat_runtime.DockerEngine("/tmp/not-a-real-docker.sock")
        engine.inspect_container = lambda identifier: existing
        return engine

    def test_create_container_adopts_existing_managed_container_on_conflict(self):
        existing = inspected_container()
        engine = self._conflict_engine(existing)

        def conflict(method, path, payload=None, **kwargs):
            raise agent_wechat_runtime.AgentWechatRuntimeError(
                "Docker Engine POST /containers/create returned 409: Conflict. "
                "The container name \"/wechat-agent-alpha-11d4b2d9\" is already in use"
            )

        with patch.object(engine, "request", side_effect=conflict):
            adopted = engine.create_container("wechat-agent-alpha-11d4b2d9", {"Image": IMAGE})
        self.assertEqual(adopted["Id"], "fake-alpha")

    def test_create_container_refuses_to_adopt_unmanaged_container(self):
        unmanaged = inspected_container()
        unmanaged["Config"]["Labels"] = {}
        engine = self._conflict_engine(unmanaged)

        def conflict(method, path, payload=None, **kwargs):
            raise agent_wechat_runtime.AgentWechatRuntimeError(
                "Docker Engine POST /containers/create returned 409: Conflict. "
                "The container name \"/wechat-agent-alpha-11d4b2d9\" is already in use"
            )

        with patch.object(engine, "request", side_effect=conflict):
            with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
                engine.create_container("wechat-agent-alpha-11d4b2d9", {"Image": IMAGE})


class LoginConvergenceTests(unittest.TestCase):
    """P0-2 — authoritative observation drives the login FSM."""

    def setUp(self):
        agent_wechat_runtime._clear_login_flow("alpha")
        self.manager = agent_wechat_runtime.AgentWechatManager(engine=object())

    def tearDown(self):
        agent_wechat_runtime._clear_login_flow("alpha")

    def _status(self, *, auth="logged_in", user="wxid_real", healthy=True, running=True):
        return {
            "running": running,
            "container_running": running,
            "agent_server_healthy": healthy,
            "runtime_health": "healthy",
            "wechat_login_status": auth,
            "logged_in_user": user,
        }

    def _install_flow(self, state, *, running=True, age=0.0):
        import time as _time

        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": running,
            "state": state,
            "qr_data_url": "",
            "logged_in_user": "",
            "error": "",
            "status_message": "",
            "updated_at": _time.time() - age,
        }
        with agent_wechat_runtime._LOGIN_FLOWS_LOCK:
            agent_wechat_runtime._LOGIN_FLOWS["alpha"] = flow
        return flow

    def test_socket_timeout_converges_to_logged_in_without_relogin(self):
        self._install_flow("timeout", running=False)
        with patch.object(self.manager, "status", return_value=self._status()):
            first = self.manager.login_status(dict(ACCOUNT))
            second = self.manager.login_status(dict(ACCOUNT))
        # Bounded: one confirmation is not enough...
        self.assertEqual(first["auth_status"], "unknown")
        # ...but the very next poll converges, with no user reload or re-login.
        self.assertEqual(second["auth_status"], "logged_in")
        self.assertEqual(second["logged_in_user"], "wxid_real")
        self.assertEqual(second["login_flow_state"], "logged_in")
        self.assertEqual(second["login_flow_source"], "observation")

    def test_lost_qr_callback_stall_converges(self):
        # The socket is "running" but has not emitted anything for longer than
        # the stall window: it is no longer a usable witness.
        self._install_flow("waiting_for_scan", running=True, age=10_000.0)
        with patch.object(self.manager, "status", return_value=self._status()):
            self.manager.login_status(dict(ACCOUNT))
            second = self.manager.login_status(dict(ACCOUNT))
        self.assertEqual(second["auth_status"], "logged_in")

    def test_live_witness_still_owns_the_login(self):
        # A fresh, running socket may still be persisting credentials; a
        # visible chat window must not finish the login on its own.
        self._install_flow("phone_confirm", running=True, age=0.0)
        with patch.object(self.manager, "status", return_value=self._status()):
            for _ in range(3):
                login = self.manager.login_status(dict(ACCOUNT))
        self.assertEqual(login["auth_status"], "unknown")
        self.assertEqual(login["logged_in_user"], "")
        self.assertEqual(login["login_flow_state"], "phone_confirm")

    def test_settle_window_bounds_convergence_with_live_witness(self):
        flow = self._install_flow("phone_confirm", running=True, age=0.0)
        with patch.object(self.manager, "status", return_value=self._status()):
            self.manager.login_status(dict(ACCOUNT))
            with flow["lock"]:
                flow["authoritative_logged_in_since"] = (
                    flow["authoritative_logged_in_since"] - agent_wechat_runtime.LOGIN_FLOW_SETTLE_SEC - 1
                )
            login = self.manager.login_status(dict(ACCOUNT))
        self.assertEqual(login["auth_status"], "logged_in")

    def test_explicit_logout_beats_stale_success_memory(self):
        self._install_flow("logged_in", running=False)
        with patch.object(
            self.manager,
            "status",
            return_value=self._status(auth="logged_out", user=""),
        ):
            login = self.manager.login_status(dict(ACCOUNT))
        self.assertEqual(login["auth_status"], "logged_out")
        self.assertEqual(login["logged_in_user"], "")

    def test_no_login_session_reports_observation_verbatim(self):
        with patch.object(self.manager, "status", return_value=self._status()):
            login = self.manager.login_status(dict(ACCOUNT))
        self.assertEqual(login["auth_status"], "logged_in")
        self.assertEqual(login["logged_in_user"], "wxid_real")

    def test_normalize_login_state(self):
        normalize = agent_wechat_runtime.normalize_login_state
        self.assertEqual(
            normalize(
                flow_state="timeout",
                auth_status="logged_in",
                logged_in_user="wxid_x",
                container_running=True,
            ),
            "WECHAT_LOGGED_IN",
        )
        self.assertEqual(
            normalize(
                flow_state="timeout",
                auth_status="unknown",
                logged_in_user="",
                container_running=True,
            ),
            "FAILED",
        )
        self.assertEqual(
            normalize(
                flow_state="waiting_for_scan",
                auth_status="unknown",
                logged_in_user="",
                container_running=True,
            ),
            "QR_READY",
        )
        self.assertEqual(
            normalize(
                flow_state="phone_confirm",
                auth_status="unknown",
                logged_in_user="",
                container_running=True,
            ),
            "PHONE_CONFIRM_PENDING",
        )
        self.assertEqual(
            normalize(
                flow_state="logged_in",
                auth_status="logged_in",
                logged_in_user="wxid_x",
                container_running=False,
            ),
            "WECHAT_LOGGED_IN",
        )


class RegisterIdempotencyTests(unittest.TestCase):
    """P0-1 — repeated "Add WeChat" must never create a second child."""

    class Registry:
        def __init__(self, accounts):
            self._accounts = accounts
            self.paths = object()

        def locked(self):
            return threading.RLock()

        def load(self, create=False):
            del create
            return {"accounts": list(self._accounts)}

    def test_repeated_register_returns_existing_account(self):
        existing = dict(ACCOUNT)
        registry = self.Registry([existing])
        with patch.object(
            wechat_runtime_control, "start_account", return_value={"running": True}
        ) as start, patch.object(
            wechat_runtime_control, "register_account"
        ) as register:
            result = wechat_runtime_control.dispatch_action(
                registry,
                {
                    "action": "register",
                    "account_id": "alpha",
                    "runtime_provider": "agent_wechat",
                },
            )
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["account"]["id"], "alpha")
        start.assert_called_once()
        register.assert_not_called()

    def test_register_with_different_provider_still_conflicts(self):
        registry = self.Registry([dict(ACCOUNT)])
        with self.assertRaises(wechat_runtime_control.RuntimeErrorWithHint):
            wechat_runtime_control.dispatch_action(
                registry,
                {"action": "register", "account_id": "alpha", "runtime_provider": "legacy"},
            )


if __name__ == "__main__":
    unittest.main()
