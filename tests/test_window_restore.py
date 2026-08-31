import importlib.util
import os
from pathlib import Path
import sys
from unittest.mock import patch
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "root"
    / "scripts"
    / "wechat"
    / "wechat_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("wechat_runtime_restore", MODULE_PATH)
wechat_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = wechat_runtime
SPEC.loader.exec_module(wechat_runtime)


class WindowRestoreTests(unittest.TestCase):
    def test_restore_prefers_main_weixin_window(self):
        account = {
            "id": "work",
            "username": "wx_work",
            "home": "/config/wechat-accounts/work/home",
            "display": ":1",
        }
        windows = [
            {"window_id": 2, "title": "wechat"},
            {"window_id": 1, "title": "Weixin"},
        ]
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return None

        with patch.object(wechat_runtime, "account_windows", return_value={"windows": windows}), patch.object(
            wechat_runtime.subprocess, "run", side_effect=fake_run
        ):
            self.assertTrue(wechat_runtime._show_account_window(account))
        self.assertEqual(
            captured["argv"],
            [
                "/scripts/wechat/wechat-display-lock.sh",
                "work",
                "xdotool",
                "windowactivate",
                "--sync",
                "1",
            ],
        )

    def test_restore_falls_back_to_first_account_window(self):
        account = {
            "id": "work",
            "username": "wx_work",
            "home": "/config/wechat-accounts/work/home",
            "display": ":1",
        }
        windows = [{"window_id": 7, "title": "wechat"}]
        captured = {}

        with patch.object(wechat_runtime, "account_windows", return_value={"windows": windows}), patch.object(
            wechat_runtime.subprocess, "run", side_effect=lambda argv, **kwargs: captured.update(argv=argv)
        ):
            self.assertTrue(wechat_runtime._show_account_window(account))
        self.assertEqual(captured["argv"][-1], "7")

    def test_restore_returns_false_when_no_window(self):
        with patch.object(wechat_runtime, "account_windows", return_value={"windows": []}):
            self.assertFalse(wechat_runtime._show_account_window({"id": "work"}))


if __name__ == "__main__":
    unittest.main()
