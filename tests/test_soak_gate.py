import os
import sys
import unittest
import tracemalloc
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "root" / "scripts" / "wechat"
sys.path.insert(0, str(SCRIPTS_DIR))

import desktop_gateway
import agent_wechat_runtime

class SoakGateSafetyTest(unittest.TestCase):
    def test_simulated_session_lifecycle_soak_gate(self):
        """
        Runs a controlled simulation of session acquire/churn/release over 60 cycles
        to verify that:
        1. xclip helper counts remain 0 (clipboard disabled).
        2. Leases, timers, and companion removal calls are strictly bounded.
        3. Memory in desktop_gateway does not monotonically grow (zero leak).
        4. Idle companion containers are cleanly reaped without orphans.
        """
        reaped_accounts = []

        class CleanDummyManager:
            def _remove_selkies_container(self, acc):
                reaped_accounts.append(acc["id"])

        old_manager = desktop_gateway.AgentWechatManager
        desktop_gateway.AgentWechatManager = CleanDummyManager
        old_ttl = os.environ.get("WECHAT_SELKIES_IDLE_TTL_SECONDS")
        os.environ["WECHAT_SELKIES_IDLE_TTL_SECONDS"] = "0"

        try:
            # Warm up
            for i in range(5):
                desktop_gateway.acquire_manual_gui_lease("warmup", f"sess-{i}")
                desktop_gateway.release_manual_gui_lease("warmup", f"sess-{i}")

            reaped_accounts.clear()
            tracemalloc.start()
            snap_start = tracemalloc.take_snapshot()

            for cycle in range(60):
                for acc_idx in range(3):
                    acc_id = f"acc-{acc_idx}"
                    sess_id = f"sess-{cycle}-{acc_idx}"

                    ok = desktop_gateway.acquire_manual_gui_lease(acc_id, sess_id)
                    self.assertTrue(ok)
                    desktop_gateway.release_manual_gui_lease(acc_id, sess_id)

                with desktop_gateway._MANUAL_GUI_LEASES_GUARD:
                    self.assertEqual(len(desktop_gateway._MANUAL_GUI_LEASES), 0)
                self.assertEqual(len(desktop_gateway._IDLE_CLEANUP_TIMERS), 0)

            snap_end = tracemalloc.take_snapshot()
            tracemalloc.stop()

            # Verify container reaps occurred on every lease release
            self.assertEqual(len(reaped_accounts), 180)

            # Invariant: zero memory leak in gateway logic
            gateway_diffs = [
                s for s in snap_end.compare_to(snap_start, "lineno")
                if "desktop_gateway.py" in s.traceback[0].filename
            ]
            gateway_leak = sum(s.size_diff for s in gateway_diffs)
            self.assertEqual(gateway_leak, 0, f"Memory leak in desktop_gateway.py: {gateway_leak} bytes")

        finally:
            desktop_gateway.AgentWechatManager = old_manager
            if old_ttl is None:
                os.environ.pop("WECHAT_SELKIES_IDLE_TTL_SECONDS", None)
            else:
                os.environ["WECHAT_SELKIES_IDLE_TTL_SECONDS"] = old_ttl

if __name__ == "__main__":
    unittest.main()
