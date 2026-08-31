import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "root"
    / "scripts"
    / "wechat"
    / "wechat_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("wechat_runtime", MODULE_PATH)
wechat_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = wechat_runtime
SPEC.loader.exec_module(wechat_runtime)


class RuntimeRegistryTests(unittest.TestCase):
    def make_paths(self, root: Path):
        return wechat_runtime.RuntimePaths(
            registry_file=root / "config" / "wechat-runtime" / "accounts.json",
            account_home_root=root / "config" / "wechat-accounts",
            runtime_dir=root / "run" / "wechat-runtime",
        )

    def test_account_id_validation_and_username(self):
        self.assertEqual(wechat_runtime.validate_account_id("work-2.test"), "work-2.test")
        self.assertTrue(wechat_runtime.account_username("work-2.test").startswith("wx_"))
        with self.assertRaises(ValueError):
            wechat_runtime.validate_account_id("../escape")
        with self.assertRaises(ValueError):
            wechat_runtime.validate_account_id("has space")

    def test_implicit_default_preserves_upstream_abc_config_home(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            env = {
                "WECHAT_ACCOUNTS": "",
                "WECHAT_LEGACY_DEFAULT_ACCOUNT": "true",
                "DISPLAY": ":1",
            }
            with patch.dict(os.environ, env, clear=False):
                data = registry.load(create=True)
            self.assertEqual(len(data["accounts"]), 1)
            account = data["accounts"][0]
            self.assertEqual(account["id"], "default")
            self.assertEqual(account["username"], "abc")
            self.assertEqual(account["home"], "/config")
            self.assertTrue(account["legacy"])

    def test_multi_account_registry_assigns_distinct_users_and_homes(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            env = {
                "WECHAT_ACCOUNTS": "default,work,personal",
                "WECHAT_LEGACY_DEFAULT_ACCOUNT": "true",
                "WECHAT_ACCOUNT_UID_BASE": "22000",
                "DISPLAY": ":1",
            }
            with patch.dict(os.environ, env, clear=False):
                data = registry.load(create=True)
            by_id = {item["id"]: item for item in data["accounts"]}
            self.assertEqual(by_id["default"]["username"], "abc")
            self.assertEqual(by_id["work"]["uid"], 22000)
            self.assertEqual(by_id["personal"]["uid"], 22001)
            self.assertNotEqual(by_id["work"]["username"], by_id["personal"]["username"])
            self.assertNotEqual(by_id["work"]["home"], by_id["personal"]["home"])
            self.assertEqual(by_id["work"]["display"], ":1")
            self.assertEqual(by_id["personal"]["display"], ":1")

    def test_display_map_overrides_selected_account(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            env = {
                "WECHAT_ACCOUNTS": "one,two",
                "WECHAT_ACCOUNT_DISPLAY_MAP": "two=:2",
                "DISPLAY": ":1",
            }
            with patch.dict(os.environ, env, clear=False):
                data = registry.load(create=True)
            by_id = {item["id"]: item for item in data["accounts"]}
            self.assertEqual(by_id["one"]["display"], ":1")
            self.assertEqual(by_id["two"]["display"], ":2")

    def test_account_environment_is_account_scoped(self):
        account = {
            "id": "work",
            "username": "wx_work",
            "uid": 22000,
            "display": ":1",
            "home": "/config/wechat-accounts/work/home",
            "legacy": False,
        }
        with patch.dict(
            os.environ,
            {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "SESSION_MANAGER": "legacy-desktop-session",
            },
            clear=False,
        ):
            env = wechat_runtime.account_environment(account)
        self.assertEqual(env["WECHAT_ACCOUNT_ID"], "work")
        self.assertEqual(env["HOME"], "/config/wechat-accounts/work/home")
        self.assertEqual(env["XDG_CONFIG_HOME"], "/config/wechat-accounts/work/home/.config")
        self.assertEqual(env["XDG_DATA_HOME"], "/config/wechat-accounts/work/home/.local/share")
        self.assertEqual(env["XDG_CACHE_HOME"], "/config/wechat-accounts/work/home/.cache")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/22000")
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertNotIn("SESSION_MANAGER", env)

    def test_display_lock_name_is_filesystem_safe(self):
        self.assertEqual(wechat_runtime.display_lock_name(":1"), "_1")
        self.assertEqual(wechat_runtime.display_lock_name("host:2.0"), "host_2.0")


if __name__ == "__main__":
    unittest.main()
