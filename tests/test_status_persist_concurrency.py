"""Deterministic concurrency regression tests for agent-status persistence in WeChat Hub Runtime.

Validates the fix for the same-account persistence race:
- R1: Two concurrent same-account persistence writers both succeed
- R2: Writers use distinct temp paths
- R3: Final target is valid JSON
- R4: No writer temp files remain after success
- R5: Writer temp file is cleaned after injected replace failure
- R6: Different accounts remain independent
- R7: Concurrent list-style and login-status-style status probes do not throw persistence exceptions
- R8: Auth status remains fail-closed: unknown is never rewritten into logged_in
- R9: Resource-drift quarantine behavior unchanged
- R10: Account scoping behavior unchanged
- Deterministic reproduction of the pre-fix shared-temp collision
- Bounded local stress test
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
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


MODULE_DIR = Path(__file__).resolve().parents[1] / "root" / "scripts" / "wechat"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

wechat_runtime = _load_module("wechat_runtime", MODULE_DIR / "wechat_runtime.py")
agent_wechat_runtime = _load_module("agent_wechat_runtime", MODULE_DIR / "agent_wechat_runtime.py")


class ReadOnlyFakeEngine:
    """Minimal fake Docker engine for status probing."""

    def __init__(self, containers=None):
        self.containers = containers or {}
        self.operations: list[tuple[str, Any]] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def socket_path(self) -> str:
        return "/var/run/docker.sock"

    def inspect_container(self, identifier: str) -> dict[str, Any] | None:
        return self.containers.get(identifier)

    def managed_containers(self, account_id: str, *, provider: str = "agent_wechat") -> list[dict[str, Any]]:
        rows = []
        for container_id, value in self.containers.items():
            labels = (value.get("Config") or {}).get("Labels") or {}
            if (
                labels.get("com.wechat-hub.managed") == "true"
                and str(labels.get("com.wechat-hub.account-id")) == str(account_id)
                and labels.get("com.wechat-hub.provider") == provider
            ):
                rows.append({"Id": container_id})
        return rows


class StatusPersistConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="runtime-test-persist-")
        self.orig_runtime_dir = os.environ.get("WECHAT_RUNTIME_DIR")
        os.environ["WECHAT_RUNTIME_DIR"] = self.test_dir
        self.account_a = {"id": "account_a", "display_name": "Account A"}
        self.account_b = {"id": "account_b", "display_name": "Account B"}

    def tearDown(self):
        if self.orig_runtime_dir is not None:
            os.environ["WECHAT_RUNTIME_DIR"] = self.orig_runtime_dir
        else:
            os.environ.pop("WECHAT_RUNTIME_DIR", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _account_dir(self, account: dict[str, Any]) -> Path:
        return Path(self.test_dir) / "accounts" / str(account["id"])

    def test_old_fixed_temp_reproduces_writer_collision(self):
        """Deterministically reproduces FileNotFoundError when two writers share a fixed temp pathname."""
        target_dir = self._account_dir(self.account_a)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "agent-status.json"
        fixed_temp = target.with_suffix(".json.tmp")

        w1_at_replace = threading.Event()
        w2_written = threading.Event()
        w1_done = threading.Event()
        caught_exceptions: list[tuple[str, Exception]] = []

        orig_replace = os.replace

        def old_writer_1():
            try:
                fixed_temp.write_text('{"writer": 1}\n', encoding="utf-8")
                w1_at_replace.set()
                self.assertTrue(w2_written.wait(timeout=5.0), "Timeout waiting for w2_written")
                orig_replace(fixed_temp, target)
                w1_done.set()
            except Exception as e:
                caught_exceptions.append(("w1", e))

        def old_writer_2():
            try:
                self.assertTrue(w1_at_replace.wait(timeout=5.0), "Timeout waiting for w1_at_replace")
                fixed_temp.write_text('{"writer": 2}\n', encoding="utf-8")
                w2_written.set()
                self.assertTrue(w1_done.wait(timeout=5.0), "Timeout waiting for w1_done")
                orig_replace(fixed_temp, target)
            except Exception as e:
                caught_exceptions.append(("w2", e))

        t1 = threading.Thread(target=old_writer_1, name="OldWriter1")
        t2 = threading.Thread(target=old_writer_2, name="OldWriter2")
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        # In the unpatched fixed-temp scheme, writer 2 deterministically fails with FileNotFoundError
        # because writer 1 already replaced (moved) fixed_temp to target.
        self.assertEqual(len(caught_exceptions), 1, f"Expected exactly 1 failure, got {caught_exceptions}")
        failed_writer, exc = caught_exceptions[0]
        self.assertEqual(failed_writer, "w2")
        self.assertIsInstance(exc, FileNotFoundError)

    def test_r1_concurrent_same_account_persistence_writers_both_succeed(self):
        """R1: Two concurrent same-account persistence writers both succeed."""
        w1_at_replace = threading.Event()
        w2_written = threading.Event()
        w1_done = threading.Event()
        errors: list[Exception] = []

        status_1 = {"wechat_login_status": "logged_in", "logged_in_user": "User 1"}
        status_2 = {"wechat_login_status": "logged_in", "logged_in_user": "User 2"}

        orig_replace = os.replace

        def synchronized_replace(src, dst):
            th = threading.current_thread().name
            if th == "R1_W1":
                w1_at_replace.set()
                w2_written.wait(timeout=5.0)
                res = orig_replace(src, dst)
                w1_done.set()
                return res
            elif th == "R1_W2":
                w2_written.set()
                w1_done.wait(timeout=5.0)
                return orig_replace(src, dst)
            else:
                return orig_replace(src, dst)

        def worker_1():
            try:
                agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, status_1)
            except Exception as e:
                errors.append(e)

        def worker_2():
            try:
                w1_at_replace.wait(timeout=5.0)
                agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, status_2)
            except Exception as e:
                errors.append(e)

        with patch("os.replace", side_effect=synchronized_replace):
            t1 = threading.Thread(target=worker_1, name="R1_W1")
            t2 = threading.Thread(target=worker_2, name="R1_W2")
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

        self.assertEqual(errors, [], f"Expected 0 errors from concurrent writers, got {errors}")

    def test_r2_writers_use_distinct_temp_paths(self):
        """R2: Writers use distinct temp paths matching the unique dotfile pattern."""
        used_temp_paths: list[Path] = []
        lock = threading.Lock()
        orig_replace = os.replace

        def record_replace(src, dst):
            with lock:
                used_temp_paths.append(Path(src))
            return orig_replace(src, dst)

        status_1 = {"payload": "first"}
        status_2 = {"payload": "second"}

        with patch("os.replace", side_effect=record_replace):
            t1 = threading.Thread(target=agent_wechat_runtime.AgentWechatManager._persist_status, args=(self.account_a, status_1))
            t2 = threading.Thread(target=agent_wechat_runtime.AgentWechatManager._persist_status, args=(self.account_a, status_2))
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

        self.assertEqual(len(used_temp_paths), 2)
        self.assertNotEqual(used_temp_paths[0], used_temp_paths[1], "Writers must use distinct temp paths")

        temp_pattern = re.compile(r"^\.agent-status\.json\.\d+\.\d+\.[0-9a-f]{16}\.tmp$")
        for path in used_temp_paths:
            self.assertTrue(
                temp_pattern.match(path.name),
                f"Temp file name {path.name} does not match expected unique pattern",
            )
            self.assertEqual(path.parent, self._account_dir(self.account_a))

    def test_r3_final_target_is_valid_json(self):
        """R3: Final target is valid JSON and contains required fields."""
        status_payload = {"wechat_login_status": "logged_in", "logged_in_user": "wxid_valid"}
        agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, status_payload)

        target = self._account_dir(self.account_a) / "agent-status.json"
        self.assertTrue(target.is_file(), "Target agent-status.json must exist")

        content = target.read_text(encoding="utf-8")
        parsed = json.loads(content)
        self.assertEqual(parsed["wechat_login_status"], "logged_in")
        self.assertEqual(parsed["logged_in_user"], "wxid_valid")
        self.assertIn("updated_at", parsed)
        self.assertIsInstance(parsed["updated_at"], int)

    def test_r4_no_writer_temp_files_remain_after_success(self):
        """R4: No writer temp files remain after success."""
        target_dir = self._account_dir(self.account_a)
        for i in range(10):
            agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, {"run": i})

        remaining_temps = list(target_dir.glob("*.tmp")) + list(target_dir.glob(".*.tmp"))
        self.assertEqual(remaining_temps, [], f"Found residual temp files: {remaining_temps}")

    def test_r5_writer_temp_file_cleaned_after_injected_replace_failure(self):
        """R5: Writer temp file is cleaned after injected replace failure."""
        target_dir = self._account_dir(self.account_a)
        target_dir.mkdir(parents=True, exist_ok=True)

        created_temp_files: list[Path] = []

        def failing_replace(src, dst):
            src_path = Path(src)
            if src_path.exists():
                created_temp_files.append(src_path)
            raise OSError("Injected disk/filesystem failure during replace")

        with patch("os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, {"test": "injected_fail"})

        self.assertTrue(len(created_temp_files) >= 1, "Temp file was expected to exist before failing replace")
        for temp_path in created_temp_files:
            self.assertFalse(temp_path.exists(), f"Temp file {temp_path} was not cleaned up after replace failure")

        remaining_temps = list(target_dir.glob("*.tmp")) + list(target_dir.glob(".*.tmp"))
        self.assertEqual(remaining_temps, [], f"Found residual temp files: {remaining_temps}")

    def test_r6_different_accounts_remain_independent(self):
        """R6: Different accounts remain independent during concurrent writes."""
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(account: dict[str, Any], payload: dict[str, Any]):
            try:
                barrier.wait(timeout=5.0)
                agent_wechat_runtime.AgentWechatManager._persist_status(account, payload)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=(self.account_a, {"account": "A"}))
        t2 = threading.Thread(target=worker, args=(self.account_b, {"account": "B"}))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        self.assertEqual(errors, [])

        target_a = self._account_dir(self.account_a) / "agent-status.json"
        target_b = self._account_dir(self.account_b) / "agent-status.json"
        self.assertTrue(target_a.is_file())
        self.assertTrue(target_b.is_file())

        parsed_a = json.loads(target_a.read_text(encoding="utf-8"))
        parsed_b = json.loads(target_b.read_text(encoding="utf-8"))
        self.assertEqual(parsed_a["account"], "A")
        self.assertEqual(parsed_b["account"], "B")

    def test_r7_concurrent_list_and_login_status_no_persistence_exceptions(self):
        """R7: Concurrent status probes do not throw persistence exceptions."""
        engine = ReadOnlyFakeEngine({
            "fake-container": {
                "Id": "fake-container",
                "State": {"Running": True},
                "Config": {
                    "Labels": {
                        "com.wechat-hub.managed": "true",
                        "com.wechat-hub.account-id": self.account_a["id"],
                        "com.wechat-hub.provider": "agent_wechat",
                    },
                    "Image": "ghcr.io/onestao/wechat-hub-agent-wechat:0.11.15-wh.1",
                },
            }
        })
        manager = agent_wechat_runtime.AgentWechatManager(engine)

        results: list[dict[str, Any]] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def probe_worker(label: str):
            try:
                barrier.wait(timeout=5.0)
                res = manager.status(self.account_a, probe_timeout=1.0)
                results.append(res)
            except Exception as e:
                errors.append(e)

        with (
            patch.object(manager, "_probe_agent_server", return_value=(True, "")),
            patch.object(manager, "_probe_wechat_login", return_value=("logged_in", "wxid_concurrent", "")),
            patch.object(manager, "_running_resource_drift_error", return_value=None),
        ):
            t1 = threading.Thread(target=probe_worker, args=("probe1",))
            t2 = threading.Thread(target=probe_worker, args=("probe2",))
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

        self.assertEqual(errors, [], f"Expected 0 probe errors, got {errors}")
        self.assertEqual(len(results), 2)
        for res in results:
            self.assertEqual(res.get("wechat_login_status"), "logged_in")
            self.assertEqual(res.get("logged_in_user"), "wxid_concurrent")

    def test_r8_auth_status_unknown_fail_closed(self):
        """R8: Auth status unknown is persisted verbatim and never rewritten to logged_in."""
        status_payload = {
            "runtime_health": "healthy",
            "wechat_login_status": "unknown",
            "logged_in_user": "",
        }
        res = agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, status_payload)
        self.assertEqual(res["wechat_login_status"], "unknown")

        target = self._account_dir(self.account_a) / "agent-status.json"
        parsed = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(parsed["wechat_login_status"], "unknown")
        self.assertEqual(parsed["logged_in_user"], "")

    def test_r9_resource_drift_quarantine_unchanged(self):
        """R9: Resource-drift quarantine status is persisted without errors."""
        status_payload = {
            "runtime_health": "degraded",
            "wechat_login_status": "quarantined-resource-drift",
            "logged_in_user": "",
            "resource_reconcile_required": True,
        }
        res = agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, status_payload)
        self.assertEqual(res["wechat_login_status"], "quarantined-resource-drift")

        target = self._account_dir(self.account_a) / "agent-status.json"
        parsed = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(parsed["wechat_login_status"], "quarantined-resource-drift")
        self.assertTrue(parsed["resource_reconcile_required"])

    def test_r10_account_scoping_behavior(self):
        """R10: Status persistence strictly respects account directory isolation."""
        agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, {"account": "A"})
        agent_wechat_runtime.AgentWechatManager._persist_status(self.account_b, {"account": "B"})

        dir_a = self._account_dir(self.account_a)
        dir_b = self._account_dir(self.account_b)

        self.assertTrue((dir_a / "agent-status.json").exists())
        self.assertTrue((dir_b / "agent-status.json").exists())
        self.assertFalse((dir_a / "accounts").exists())
        self.assertFalse((dir_b / "accounts").exists())

    def test_stress_same_account_persistence_concurrency(self):
        """Bounded stress test: multiple threads hammering same-account persistence concurrently."""
        num_threads = 6
        iterations_per_thread = 15
        errors: list[Exception] = []

        def stress_worker(thread_id: int):
            for i in range(iterations_per_thread):
                try:
                    payload = {
                        "thread_id": thread_id,
                        "iteration": i,
                        "timestamp": time.time(),
                        "wechat_login_status": "logged_in",
                    }
                    agent_wechat_runtime.AgentWechatManager._persist_status(self.account_a, payload)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=stress_worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [], f"Stress test encountered errors: {errors}")

        target = self._account_dir(self.account_a) / "agent-status.json"
        self.assertTrue(target.is_file())
        parsed = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("thread_id", parsed)
        self.assertIn("iteration", parsed)

        remaining_temps = list(self._account_dir(self.account_a).glob("*.tmp")) + list(
            self._account_dir(self.account_a).glob(".*.tmp")
        )
        self.assertEqual(remaining_temps, [], f"Residual temps after stress test: {remaining_temps}")


if __name__ == "__main__":
    unittest.main()
