import importlib.util
import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
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

CONTROL_MODULE_PATH = MODULE_PATH.with_name("wechat_runtime_control.py")
CONTROL_SPEC = importlib.util.spec_from_file_location("wechat_runtime_control", CONTROL_MODULE_PATH)
wechat_runtime_control = importlib.util.module_from_spec(CONTROL_SPEC)
assert CONTROL_SPEC.loader is not None
sys.modules[CONTROL_SPEC.name] = wechat_runtime_control
CONTROL_SPEC.loader.exec_module(wechat_runtime_control)

AGENT_MODULE_PATH = MODULE_PATH.with_name("agent_wechat_runtime.py")
AGENT_SPEC = importlib.util.spec_from_file_location("agent_wechat_runtime", AGENT_MODULE_PATH)
agent_wechat_runtime = importlib.util.module_from_spec(AGENT_SPEC)
assert AGENT_SPEC.loader is not None
sys.modules[AGENT_SPEC.name] = agent_wechat_runtime
AGENT_SPEC.loader.exec_module(agent_wechat_runtime)

GATEWAY_MODULE_PATH = MODULE_PATH.with_name("desktop_gateway.py")
GATEWAY_SPEC = importlib.util.spec_from_file_location("desktop_gateway", GATEWAY_MODULE_PATH)
desktop_gateway = importlib.util.module_from_spec(GATEWAY_SPEC)
assert GATEWAY_SPEC.loader is not None
sys.modules[GATEWAY_SPEC.name] = desktop_gateway
GATEWAY_SPEC.loader.exec_module(desktop_gateway)

SELKIES_GATEWAY_MODULE_PATH = MODULE_PATH.with_name("selkies_attach_gateway.py")
SELKIES_GATEWAY_SPEC = importlib.util.spec_from_file_location(
    "selkies_attach_gateway", SELKIES_GATEWAY_MODULE_PATH
)
selkies_attach_gateway = importlib.util.module_from_spec(SELKIES_GATEWAY_SPEC)
assert SELKIES_GATEWAY_SPEC.loader is not None
sys.modules[SELKIES_GATEWAY_SPEC.name] = selkies_attach_gateway
SELKIES_GATEWAY_SPEC.loader.exec_module(selkies_attach_gateway)


class FakeDockerEngine:
    def __init__(self):
        self.containers = {}
        self.volumes = {}
        self.operations = []
        self._next_id = 1

    @property
    def available(self):
        return True

    def _resolve(self, identifier):
        if identifier in self.containers:
            return identifier
        for container_id, value in self.containers.items():
            if str(value.get("Name") or "").lstrip("/") == identifier:
                return container_id
        return None

    def inspect_container(self, identifier):
        resolved = self._resolve(identifier)
        return self.containers.get(resolved) if resolved else None

    def managed_containers(self, account_id, *, provider="agent_wechat"):
        rows = []
        for container_id, value in self.containers.items():
            labels = value.get("Config", {}).get("Labels", {})
            if (
                labels.get("com.wechat-hub.managed") == "true"
                and labels.get("com.wechat-hub.account-id") == account_id
                and labels.get("com.wechat-hub.provider") == provider
            ):
                rows.append({"Id": container_id, "Labels": labels})
        return rows

    def create_volume(self, name, device, labels):
        self.volumes[name] = {"device": device, "labels": dict(labels)}
        self.operations.append(("create_volume", name))

    def remove_volume(self, name):
        self.volumes.pop(name, None)
        self.operations.append(("remove_volume", name))

    def create_container(self, name, payload):
        container_id = f"fake-{self._next_id}"
        self._next_id += 1
        self.containers[container_id] = {
            "Id": container_id,
            "Name": f"/{name}",
            "Image": payload["Image"],
            "Config": {
                "Image": payload["Image"],
                "Labels": dict(payload.get("Labels") or {}),
                "Env": list(payload.get("Env") or []),
                "Entrypoint": list(payload.get("Entrypoint") or []),
                "Cmd": list(payload.get("Cmd") or []),
            },
            "HostConfig": dict(payload.get("HostConfig") or {}),
            "State": {"Running": False},
            "NetworkSettings": {"Ports": {}},
        }
        self.operations.append(("create_container", name, payload["Image"]))
        return {"Id": container_id}

    def start_container(self, identifier):
        resolved = self._resolve(identifier)
        self.containers[resolved]["State"]["Running"] = True
        self.operations.append(("start_container", resolved))

    def stop_container(self, identifier, timeout=10):
        del timeout
        resolved = self._resolve(identifier)
        self.containers[resolved]["State"]["Running"] = False
        self.operations.append(("stop_container", resolved))

    def remove_container(self, identifier, force=False):
        del force
        resolved = self._resolve(identifier)
        if resolved:
            name = str(self.containers[resolved].get("Name") or "").lstrip("/")
            self.containers.pop(resolved, None)
            self.operations.append(("remove_container", resolved, name))

    def exec_container(self, identifier, command, *, env=None, timeout=30.0, attach_stderr=True):
        del env, timeout, attach_stderr
        resolved = self._resolve(identifier)
        if not resolved:
            raise RuntimeError("container not found")
        self.operations.append(("exec_container", resolved, list(command)))
        return 0, b"state=restarted\n"


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

    def test_runtime_resource_name_is_docker_safe_and_collision_resistant(self):
        first = wechat_runtime.sanitize_account_runtime_name("Work.Team")
        second = wechat_runtime.sanitize_account_runtime_name("work-team")
        self.assertRegex(first, r"^[a-z0-9-]+-[0-9a-f]{8}$")
        self.assertRegex(second, r"^[a-z0-9-]+-[0-9a-f]{8}$")
        self.assertNotEqual(first, second)

    def test_agent_provider_registry_is_isolated_and_default_remove_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            with patch.dict(
                os.environ,
                {"WECHAT_ACCOUNTS": "base", "WECHAT_LEGACY_DEFAULT_ACCOUNT": "false", "DISPLAY": ":1"},
                clear=False,
            ), patch.object(wechat_runtime, "require_root"), patch.object(
                wechat_runtime, "bootstrap_account", return_value=False
            ):
                registry.load(create=True)
                account = wechat_runtime.register_account(
                    registry,
                    "work.agent",
                    None,
                    True,
                    "Work Agent",
                    "agent_wechat",
                )

            self.assertEqual(account["runtime_provider"], "agent_wechat")
            self.assertIsNone(account["uid"])
            self.assertEqual(account["display"], "isolated")
            self.assertTrue(account["agent_wechat"]["container_name"].startswith("wechat-agent-"))
            self.assertIn("/config/agent-wechat/", account["home"])
            self.assertNotEqual(account["home"], str(paths.account_home_root / "work.agent" / "home"))

            removal = {
                "removed": "work.agent",
                "runtime_provider": "agent_wechat",
                "preserve_data": True,
            }
            with patch.object(wechat_runtime, "require_root"), patch.object(
                agent_wechat_runtime.AgentWechatManager, "remove", return_value=removal
            ) as remove:
                result = wechat_runtime.unregister_account(registry, "work.agent")
            self.assertTrue(result["preserve_data"])
            remove.assert_called_once()
            self.assertFalse(remove.call_args.kwargs["purge_data"])

    def test_agent_container_payload_uses_labels_named_volumes_and_isolated_pid_namespace(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        payload = manager._container_payload(
            account,
            network="wechat-hub_default",
            data_volume="personal-data",
            home_volume="personal-home",
            host_token="/host/config/personal/auth-token",
        )
        self.assertNotIn("PidMode", payload["HostConfig"])
        self.assertNotIn("PortBindings", payload["HostConfig"])
        self.assertEqual(payload["HostConfig"]["NetworkMode"], "wechat-hub_default")
        self.assertEqual(payload["Labels"]["com.wechat-hub.account-id"], "personal")
        self.assertEqual(payload["Labels"]["com.wechat-hub.provider"], "agent_wechat")
        mounts = payload["HostConfig"]["Mounts"]
        self.assertTrue(any(item.get("Source") == "personal-data" and item.get("Target") == "/data" for item in mounts))
        self.assertTrue(any(item.get("Source") == "personal-home" and item.get("Target") == "/home/wechat" for item in mounts))
        self.assertTrue(any(item.get("Target") == "/data/auth-token" and item.get("ReadOnly") for item in mounts))
        self.assertEqual(payload["HostConfig"]["PidsLimit"], 256)
        self.assertEqual(payload["HostConfig"]["Memory"], 2048 * 1024 * 1024)

    def test_agent_health_distinguishes_container_server_and_login_state(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        inspected = {
            "Id": "fake-personal",
            "Name": "/wechat-agent-personal",
            "Config": {"Image": "ghcr.io/thisnick/agent-wechat:0.11.15"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {}},
        }
        base = manager._status_from_inspect(account, inspected)
        with patch.object(manager, "_probe_agent_server", return_value=(False, "health timeout")):
            degraded = manager._enrich_health(account, dict(base))
        self.assertTrue(degraded["container_running"])
        self.assertFalse(degraded["agent_server_healthy"])
        self.assertEqual(degraded["runtime_health"], "degraded")
        self.assertEqual(degraded["wechat_login_status"], "unknown")

        with patch.object(manager, "_probe_agent_server", return_value=(True, "")), patch.object(
            manager, "_probe_wechat_login", return_value=("logged_out", "", "")
        ):
            healthy = manager._enrich_health(account, dict(base))
        self.assertTrue(healthy["container_running"])
        self.assertTrue(healthy["agent_server_healthy"])
        self.assertEqual(healthy["runtime_health"], "healthy")
        self.assertEqual(healthy["wechat_login_status"], "logged_out")

    def test_interactive_desktop_exec_is_fixed_account_scoped_and_localhost_only(self):
        engine = FakeDockerEngine()
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        container_id = "fake-alpha"
        engine.containers[container_id] = {
            "Id": container_id,
            "Name": "/wechat-agent-alpha",
            "Config": {
                "Image": "ghcr.io/thisnick/agent-wechat:0.11.15",
                "Labels": agent_wechat_runtime._labels("alpha"),
            },
            "HostConfig": {"NetworkMode": "hub-net"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {}},
        }
        manager = agent_wechat_runtime.AgentWechatManager(engine=engine)
        result = manager.ensure_interactive_desktop(account)
        self.assertTrue(result["interactive"])
        self.assertEqual(result["display"], ":99")
        self.assertEqual(result["rfbport"], 5900)
        self.assertEqual(result["listen"], "127.0.0.1")
        exec_ops = [item for item in engine.operations if item[0] == "exec_container"]
        self.assertEqual(len(exec_ops), 1)
        self.assertEqual(exec_ops[0][1], container_id)
        self.assertEqual(exec_ops[0][2], agent_wechat_runtime.INTERACTIVE_DESKTOP_COMMAND)
        script = exec_ops[0][2][2]
        launch = next(line.strip() for line in script.splitlines() if line.strip().startswith("nohup x11vnc"))
        self.assertIn("-display :99", launch)
        self.assertIn("-rfbport 5900", launch)
        self.assertIn("-listen 127.0.0.1", launch)
        self.assertIn("-nopw", launch)
        self.assertIn("-shared", launch)
        self.assertIn("-forever", launch)
        self.assertIn("-xkb", launch)
        self.assertNotIn("-viewonly", launch)

    def test_interactive_desktop_refuses_mismatched_container_labels(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        inspected = {
            "Id": "fake-alpha",
            "Config": {
                "Labels": {
                    "com.wechat-hub.managed": "true",
                    "com.wechat-hub.account-id": "beta",
                    "com.wechat-hub.provider": "agent_wechat",
                }
            },
            "State": {"Running": True},
        }
        with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
            agent_wechat_runtime.AgentWechatManager._validate_managed_container(account, inspected)

    def test_selkies_companion_is_display_only_account_scoped_and_has_no_host_port(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        with patch.object(manager, "selkies_image_for", return_value="sha256:runtime-image"):
            payload = manager._selkies_payload(
                account,
                parent_container_id="agent-alpha-id",
                x11_volume="alpha-x11",
                browser_files_volume="alpha-files",
                host_desktop_token="/host/alpha/desktop-auth-token",
            )

        self.assertEqual(payload["Image"], "sha256:runtime-image")
        self.assertEqual(payload["Entrypoint"], ["/bin/bash", "-c"])
        command = payload["Cmd"][0]
        self.assertIn("selkies --addr=127.0.0.1", command)
        self.assertIn("--mode=websockets", command)
        self.assertIn("--enable-resize=true", command)
        self.assertIn("--addr=127.0.0.1", command)
        self.assertIn("--port=8082", command)
        self.assertIn("--enable-basic-auth=false", command)
        self.assertIn("python3 /scripts/wechat/selkies_attach_gateway.py &", command)
        self.assertIn('wait -n "$selkies_pid" "$proxy_pid"', command)
        self.assertNotIn("exec python3 /scripts/wechat/selkies_attach_gateway.py", command)
        self.assertNotIn("pkill -u wechat xclip", command)
        self.assertNotIn("Xvfb", command)
        self.assertNotIn("/usr/bin/wechat", command)
        host = payload["HostConfig"]
        self.assertEqual(host["IpcMode"], "container:agent-alpha-id")
        self.assertEqual(host["NetworkMode"], "container:agent-alpha-id")
        self.assertTrue(host["Init"])
        self.assertNotIn("PortBindings", host)
        mounts = host["Mounts"]
        self.assertIn(
            {"Type": "volume", "Source": "alpha-x11", "Target": "/tmp/.X11-unix"},
            mounts,
        )
        self.assertIn(
            {"Type": "volume", "Source": "alpha-files", "Target": "/config"},
            mounts,
        )
        self.assertIn(
            {
                "Type": "bind",
                "Source": "/host/alpha/desktop-auth-token",
                "Target": "/run/secrets/wechat-hub-desktop-token",
                "ReadOnly": True,
            },
            mounts,
        )
        self.assertEqual(payload["Labels"]["com.wechat-hub.account-id"], "alpha")
        self.assertEqual(payload["Labels"]["com.wechat-hub.provider"], "agent_wechat_selkies")
        env = set(payload["Env"])
        self.assertIn("DISPLAY=:99", env)
        self.assertIn("SELKIES_CLIPBOARD_ENABLED=false|locked", env)
        self.assertIn("SELKIES_ENABLE_BINARY_CLIPBOARD=false|locked", env)
        self.assertIn("SELKIES_FILE_TRANSFERS=upload,download", env)
        self.assertIn("SELKIES_COMMAND_ENABLED=false|locked", env)
        self.assertIn("SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD=false|locked", env)
        self.assertIn("SELKIES_UI_SIDEBAR_SHOW_SCREEN_SETTINGS=true|locked", env)
        self.assertFalse(any(item.startswith("SELKIES_BASIC_AUTH_PASSWORD=") for item in env))
        self.assertEqual(host["PidsLimit"], 100)
        self.assertEqual(host["Memory"], 1024 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 2000000000)

    def test_selkies_health_probe_uses_private_desktop_header_not_basic_auth(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return b"ok"

        def urlopen(request, timeout=0):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch.object(manager, "_desktop_token", return_value="d" * 64), patch.object(
            agent_wechat_runtime.urllib.request, "urlopen", side_effect=urlopen
        ):
            healthy, error = manager._probe_selkies(account, timeout=1.5)

        self.assertTrue(healthy)
        self.assertEqual(error, "")
        self.assertGreater(captured["timeout"], 0)
        headers = {key.lower(): value for key, value in captured["request"].header_items()}
        self.assertEqual(headers["x-wechat-hub-desktop-token"], "d" * 64)
        self.assertNotIn("authorization", headers)

    def test_selkies_health_probe_accepts_426_upgrade_required(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())

        def urlopen(request, timeout=0):
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, 426, "Upgrade Required", {}, None)

        with patch.object(manager, "_desktop_token", return_value="d" * 64), patch.object(
            agent_wechat_runtime.urllib.request, "urlopen", side_effect=urlopen
        ):
            healthy, error = manager._probe_selkies(account, timeout=1.5)

        self.assertTrue(healthy)
        self.assertEqual(error, "")

    def test_x11_socket_reset_does_not_touch_persistent_account_files(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(manager, "runtime_storage_root", return_value=root):
                files = manager.prepare_files(account)
                x11 = Path(files["x11"])
                browser_files = Path(files["browser_files"])
                data = Path(files["data"])
                (x11 / "X99").write_text("stale", encoding="utf-8")
                (browser_files / "keep.txt").write_text("keep", encoding="utf-8")
                (data / "keep.db").write_text("keep", encoding="utf-8")
                manager._reset_x11_socket_dir(account)

                self.assertEqual(list(x11.iterdir()), [])
                self.assertEqual((browser_files / "keep.txt").read_text(encoding="utf-8"), "keep")
                self.assertEqual((data / "keep.db").read_text(encoding="utf-8"), "keep")

    def test_selkies_desktop_descriptor_is_opaque_and_advertises_rich_input_features(self):
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "WECHAT_DESKTOP_GATEWAY_SESSION_DIR": temp,
                "WECHAT_DESKTOP_GATEWAY_PORT": "17892",
                "WECHAT_DESKTOP_GATEWAY_PUBLIC_SCHEME": "https",
                "WECHAT_DESKTOP_GATEWAY_PUBLIC_HOST": "wechat.example.test",
                "WECHAT_DESKTOP_GATEWAY_PUBLIC_PORT": "443",
                "WECHAT_SELKIES_ATTACH_ENABLED": "true",
            },
            clear=False,
        ), patch.object(
            manager,
            "status",
            return_value={
                "running": True,
                "container_running": True,
                "agent_server_healthy": True,
            },
        ), patch.object(
            manager,
            "ensure_selkies_desktop",
            return_value={
                "desktop_provider": "selkies",
                "features": dict(agent_wechat_runtime.SELKIES_DESKTOP_FEATURES),
            },
        ):
            desktop = manager.desktop(account)

            self.assertEqual(desktop["desktop_provider"], "selkies")
            self.assertEqual(desktop["scheme"], "https")
            self.assertEqual(desktop["host"], "wechat.example.test")
            self.assertEqual(desktop["port"], 443)
            self.assertRegex(desktop["path"], r"^/desktop/[A-Za-z0-9_-]+/$")
            self.assertNotIn("token=", desktop["path"])
            self.assertTrue(desktop["features"]["local_ime"])
            self.assertFalse(desktop["features"]["clipboard_text"])
            self.assertFalse(desktop["features"]["clipboard_image"])
            self.assertTrue(desktop["features"]["file_upload"])
            self.assertTrue(desktop["features"]["dynamic_resize"])
            descriptors = [json.loads(path.read_text(encoding="utf-8")) for path in Path(temp).glob("*.json")]
            self.assertEqual(len(descriptors), 1)
            self.assertEqual(descriptors[0]["desktop_provider"], "selkies")
            self.assertNotIn("token", descriptors[0])

    def test_desktop_auto_falls_back_to_novnc_without_recreating_old_live_account(self):
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "WECHAT_DESKTOP_GATEWAY_SESSION_DIR": temp,
                "WECHAT_SELKIES_ATTACH_ENABLED": "true",
            },
            clear=False,
        ), patch.object(
            manager,
            "status",
            return_value={"container_running": True, "agent_server_healthy": True},
        ), patch.object(
            manager,
            "ensure_selkies_desktop",
            side_effect=agent_wechat_runtime.AgentWechatRuntimeError(
                "Selkies desktop requires one normal account restart"
            ),
        ), patch.object(
            manager,
            "ensure_interactive_desktop",
            return_value={"interactive": True},
        ):
            desktop = manager.desktop(account)

        self.assertEqual(desktop["desktop_provider"], "novnc")
        self.assertIn("/vnc/", desktop["path"])
        self.assertIn("restart", desktop["fallback_reason"])
        self.assertFalse(desktop["features"]["local_ime"])
        self.assertFalse(desktop["features"]["file_upload"])

    def test_selkies_companion_lifecycle_never_creates_a_second_agent_wechat_runtime(self):
        engine = FakeDockerEngine()
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        primary_id = "agent-alpha-id"
        engine.containers[primary_id] = {
            "Id": primary_id,
            "Name": "/wechat-agent-alpha",
            "Image": "ghcr.io/thisnick/agent-wechat:0.11.15",
            "Config": {
                "Image": "ghcr.io/thisnick/agent-wechat:0.11.15",
                "Labels": agent_wechat_runtime._labels("alpha"),
            },
            "HostConfig": {
                "NetworkMode": "hub-net",
                "Mounts": [
                    {"Type": "volume", "Source": "alpha-x11", "Target": "/tmp/.X11-unix"},
                    {
                        "Type": "volume",
                        "Source": "alpha-files",
                        "Target": "/home/wechat/WeChatHubFiles",
                    },
                ],
            },
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {}},
        }
        manager = agent_wechat_runtime.AgentWechatManager(engine=engine)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def desktop_volumes(_account, _host_root):
                engine.create_volume("alpha-x11", str(root / "x11"), {})
                engine.create_volume("alpha-files", str(root / "files"), {})
                return "alpha-x11", "alpha-files"

            with patch.object(
                manager, "_host_config_root_and_network", return_value=(str(root), "hub-net")
            ), patch.object(
                manager, "_ensure_desktop_volumes", side_effect=desktop_volumes
            ), patch.object(
                manager, "selkies_image_for", return_value="sha256:runtime-image"
            ), patch.object(
                manager, "_probe_selkies", return_value=(True, "")
            ):
                desktop = manager.ensure_selkies_desktop(account)

        self.assertEqual(desktop["desktop_provider"], "selkies")
        sidecars = engine.managed_containers("alpha", provider="agent_wechat_selkies")
        primaries = engine.managed_containers("alpha", provider="agent_wechat")
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0]["Id"], primary_id)
        sidecar = engine.inspect_container(sidecars[0]["Id"])
        self.assertTrue(sidecar["State"]["Running"])
        self.assertEqual(sidecar["HostConfig"]["IpcMode"], f"container:{primary_id}")
        self.assertNotIn("PortBindings", sidecar["HostConfig"])
        self.assertEqual(
            sidecar["Config"]["Labels"]["com.wechat-hub.parent-container"], primary_id
        )

        manager._remove_selkies_container(account)
        self.assertEqual(engine.managed_containers("alpha", provider="agent_wechat_selkies"), [])
        self.assertTrue(engine.inspect_container(primary_id)["State"]["Running"])

    def test_desktop_gateway_descriptor_never_exposes_upstream_token(self):
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        account_a = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        account_b = {"id": "beta", "display_name": "Beta", "runtime_provider": "agent_wechat"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "WECHAT_DESKTOP_GATEWAY_SESSION_DIR": temp,
                "WECHAT_DESKTOP_GATEWAY_PORT": "17892",
                "WECHAT_SELKIES_ATTACH_ENABLED": "false",
            },
            clear=False,
        ), patch.object(
            manager,
            "status",
            return_value={
                "running": True,
                "container_running": True,
                "agent_server_healthy": True,
            },
        ), patch.object(
            manager,
            "ensure_interactive_desktop",
            return_value={"interactive": True, "action": "interactive"},
        ):
            desktop_a = manager.desktop(account_a)
            desktop_b = manager.desktop(account_b)

            self.assertNotEqual(desktop_a["path"], desktop_b["path"])
            self.assertNotIn("token=", desktop_a["path"])
            self.assertNotIn("token=", desktop_b["path"])
            # The advertised browser WebSocket path must resolve to upstream's
            # only VNC route once the Gateway strips the /desktop/<session> prefix.
            self.assertIn("%2Fvnc%2Fwebsockify", desktop_a["path"])
            self.assertIn("/vnc/?autoconnect=true", desktop_a["path"])
            self.assertEqual(desktop_a["port"], 17892)
            descriptors = [json.loads(path.read_text(encoding="utf-8")) for path in Path(temp).glob("*.json")]
            self.assertEqual({item["account_id"] for item in descriptors}, {"alpha", "beta"})
            self.assertTrue(all("token" not in item for item in descriptors))

            class FakeRegistry:
                def __init__(self, _paths):
                    pass

                def load(self, create=False):
                    del create
                    return {"accounts": [account_a, account_b]}

            session_a = desktop_a["path"].split("/desktop/", 1)[1].split("/", 1)[0]
            session_b = desktop_b["path"].split("/desktop/", 1)[1].split("/", 1)[0]
            with patch.object(desktop_gateway, "Registry", FakeRegistry):
                _, resolved_a = desktop_gateway.load_session(session_a)
                _, resolved_b = desktop_gateway.load_session(session_b)
            self.assertEqual(resolved_a["id"], "alpha")
            self.assertEqual(resolved_b["id"], "beta")

        class Request:
            headers = {"Authorization": "Bearer browser-value", "X-Test": "yes"}

        with patch.object(agent_wechat_runtime.AgentWechatManager, "_token", return_value="upstream-secret"):
            upstream_host = agent_wechat_runtime.AgentWechatManager.container_name(account_a)
            internal_http = desktop_gateway.upstream_url(
                account_a, "vnc/", {"token": "browser-value", "autoconnect": "true"}
            )
            internal_websocket = desktop_gateway.upstream_url(
                account_a,
                "vnc/websockify",
                {"token": "browser-value", "autoconnect": "true"},
                websocket=True,
            )
            headers = desktop_gateway.upstream_headers(Request(), account_a)
            rewritten = desktop_gateway.rewrite_location(
                f"http://{upstream_host}:6174/vnc/?token=secret", "session-id", account_a
            )
            incident_redirect = desktop_gateway.rewrite_location(
                f"http://{upstream_host}:6174/vnc/"
                "?autoconnect=true&path=vnc%2Fwebsockify%3Ftoken%3Dupstream-secret"
                "&token=upstream-secret",
                "session-id",
                account_a,
            )
        self.assertNotIn("token=", internal_http)
        self.assertNotIn("browser-value", internal_http)
        self.assertIn("token=upstream-secret", internal_websocket)
        self.assertNotIn("browser-value", internal_websocket)
        self.assertEqual(headers["Authorization"], "Bearer upstream-secret")
        self.assertNotIn("upstream-secret", rewritten)
        self.assertEqual(
            incident_redirect,
            "/desktop/session-id/vnc/?autoconnect=true&path=desktop%2Fsession-id%2Fvnc%2Fwebsockify",
        )
        self.assertNotIn("token=", urllib.parse.unquote_plus(incident_redirect))

    def test_desktop_gateway_serves_novnc_client_without_upstream_token_gate(self):
        # Upstream only renders the real noVNC client when its token is present
        # in the browser URL, so the Gateway must request vnc.html instead.
        self.assertEqual(desktop_gateway.landing_tail("vnc/"), "vnc/vnc.html")
        self.assertEqual(desktop_gateway.landing_tail("vnc"), "vnc/vnc.html")
        self.assertEqual(desktop_gateway.landing_tail("VNC/"), "vnc/vnc.html")
        self.assertEqual(desktop_gateway.landing_tail("vnc/app/ui.js"), "vnc/app/ui.js")
        self.assertEqual(desktop_gateway.landing_tail("vnc/websockify"), "vnc/websockify")
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        with patch.object(
            agent_wechat_runtime.AgentWechatManager, "_token", return_value="upstream-secret"
        ):
            url = desktop_gateway.upstream_url(
                account,
                desktop_gateway.landing_tail("vnc/"),
                {"autoconnect": "true", "path": "desktop/session-id/vnc/websockify"},
            )
            ws_url = desktop_gateway.upstream_url(account, "vnc/websockify", {}, websocket=True)
        self.assertIn("/vnc/vnc.html?", url)
        self.assertNotIn("token=", url)
        self.assertNotIn("upstream-secret", url)
        self.assertIn("/vnc/websockify?", ws_url)
        self.assertIn("token=upstream-secret", ws_url)

    def test_manual_desktop_gui_lease_is_reentrant_per_session_and_account_scoped(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"WECHAT_GUI_LEASE_DIR": temp}, clear=False
        ):
            self.assertTrue(desktop_gateway.acquire_manual_gui_lease("alpha", "session-a"))
            self.assertTrue(desktop_gateway.acquire_manual_gui_lease("alpha", "session-a"))
            self.assertFalse(desktop_gateway.acquire_manual_gui_lease("alpha", "session-b"))
            self.assertTrue(desktop_gateway.acquire_manual_gui_lease("beta", "session-b"))

            desktop_gateway.release_manual_gui_lease("alpha", "session-a")
            self.assertFalse(desktop_gateway.acquire_manual_gui_lease("alpha", "session-b"))
            desktop_gateway.release_manual_gui_lease("alpha", "session-a")
            self.assertTrue(desktop_gateway.acquire_manual_gui_lease("alpha", "session-b"))

            desktop_gateway.release_manual_gui_lease("alpha", "session-b")
            desktop_gateway.release_manual_gui_lease("beta", "session-b")

        self.assertEqual(desktop_gateway._MANUAL_GUI_LEASES, {})

    def test_selkies_gateway_uses_internal_companion_without_agent_token(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        descriptor = {"desktop_provider": "selkies"}

        class Request:
            headers = {
                "Authorization": "Bearer browser-value",
                "Cookie": "browser=cookie",
                "X-Test": "yes",
            }
            scheme = "https"

        with patch.object(
            agent_wechat_runtime.AgentWechatManager,
            "_desktop_token",
            return_value="d" * 64,
        ):
            url = desktop_gateway.proxy_upstream_url(
                descriptor,
                account,
                "websocket",
                {"token": "browser-value", "q": "1"},
                websocket=True,
            )
            headers = desktop_gateway.proxy_upstream_headers(
                Request(), descriptor, account, "opaque-session"
            )
        desktop_host = agent_wechat_runtime.AgentWechatManager.container_name(account)
        rewritten = desktop_gateway.rewrite_location(
            f"http://{desktop_host}:8081/settings?q=1",
            "opaque-session",
            account,
            provider="selkies",
        )

        self.assertIn(f"http://{desktop_host}:8081/websocket?q=1", url)
        self.assertNotIn("token=", url)
        self.assertNotIn("browser-value", url)
        self.assertNotIn("Cookie", headers)
        self.assertEqual(headers["X-Test"], "yes")
        self.assertEqual(headers["X-Forwarded-Prefix"], "/desktop/opaque-session/")
        self.assertEqual(headers["X-Forwarded-Proto"], "https")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["X-WeChat-Hub-Desktop-Token"], "d" * 64)
        self.assertNotIn("browser-value", headers["X-WeChat-Hub-Desktop-Token"])
        self.assertEqual(rewritten, "/desktop/opaque-session/settings?q=1")

    def test_selkies_internal_proxy_validates_and_strips_desktop_secret(self):
        secret = "d" * 64

        class RelUrl:
            def __str__(self):
                return "/websocket?q=1"

        class Request:
            rel_url = RelUrl()
            headers = {
                "X-WeChat-Hub-Desktop-Token": secret,
                "Authorization": "Bearer browser-value",
                "Cookie": "browser=cookie",
                "X-Forwarded-Prefix": "/desktop/session/",
                "X-Test": "yes",
            }

        request = Request()
        self.assertTrue(selkies_attach_gateway.authorized(request, secret))
        self.assertFalse(selkies_attach_gateway.authorized(request, "e" * 64))
        forwarded = selkies_attach_gateway.request_headers(request)
        self.assertNotIn("X-WeChat-Hub-Desktop-Token", forwarded)
        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("Cookie", forwarded)
        self.assertEqual(forwarded["X-Forwarded-Prefix"], "/desktop/session/")
        self.assertEqual(forwarded["X-Test"], "yes")
        self.assertEqual(
            selkies_attach_gateway.upstream_url(request),
            "http://127.0.0.1:8082/websocket?q=1",
        )

    def test_fake_docker_two_account_lifecycle_never_crosses_account_resources(self):
        engine = FakeDockerEngine()
        manager = agent_wechat_runtime.AgentWechatManager(engine=engine)
        account_a = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        account_b = {"id": "beta", "display_name": "Beta", "runtime_provider": "agent_wechat"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def storage_root(account):
                return root / str(account["id"])

            def ensure_volumes(account, _host_config_root):
                files = manager.prepare_files(account)
                data_volume, home_volume = manager.storage_names(account)
                labels = agent_wechat_runtime._labels(str(account["id"]))
                engine.create_volume(data_volume, files["data"], labels)
                engine.create_volume(home_volume, files["home"], labels)
                return data_volume, home_volume, files["token"]

            def ensure_desktop_volumes(account, _host_config_root):
                files = manager.prepare_files(account)
                x11_volume, browser_files_volume = manager.desktop_storage_names(account)
                labels = agent_wechat_runtime._labels(str(account["id"]))
                engine.create_volume(x11_volume, files["x11"], labels)
                engine.create_volume(browser_files_volume, files["browser_files"], labels)
                return x11_volume, browser_files_volume

            def healthy(_account, status):
                status["container_running"] = bool(status.get("running"))
                status["agent_server_healthy"] = True if status.get("running") else None
                status["runtime_health"] = "healthy" if status.get("running") else "stopped"
                status["wechat_login_status"] = "logged_out" if status.get("running") else "stopped"
                return status

            with patch.object(manager, "runtime_storage_root", side_effect=storage_root), patch.object(
                manager, "_host_config_root_and_network", return_value=(str(root), "hub-net")
            ), patch.object(manager, "_ensure_volumes", side_effect=ensure_volumes), patch.object(
                manager, "_ensure_desktop_volumes", side_effect=ensure_desktop_volumes
            ), patch.object(
                manager, "_enrich_health", side_effect=healthy
            ), patch.dict(
                os.environ, {"WECHAT_RUNTIME_DIR": str(root / "runtime")}, clear=False
            ):
                token_a = Path(manager.prepare_files(account_a)["token"]).read_text(encoding="utf-8").strip()
                token_b = Path(manager.prepare_files(account_b)["token"]).read_text(encoding="utf-8").strip()
                desktop_token_a = Path(manager.prepare_files(account_a)["desktop_token"]).read_text(encoding="utf-8").strip()
                desktop_token_b = Path(manager.prepare_files(account_b)["desktop_token"]).read_text(encoding="utf-8").strip()
                self.assertRegex(token_a, r"^[0-9a-f]{64}$")
                self.assertRegex(token_b, r"^[0-9a-f]{64}$")
                self.assertNotEqual(token_a, token_b)
                self.assertRegex(desktop_token_a, r"^[0-9a-f]{64}$")
                self.assertRegex(desktop_token_b, r"^[0-9a-f]{64}$")
                self.assertNotEqual(desktop_token_a, desktop_token_b)
                self.assertNotEqual(token_a, desktop_token_a)
                self.assertNotEqual(token_b, desktop_token_b)

                started_a = manager.start(account_a)
                started_b = manager.start(account_b)
                id_a = started_a["container_id"]
                id_b = started_b["container_id"]
                self.assertNotEqual(id_a, id_b)
                self.assertTrue(engine.inspect_container(id_a)["State"]["Running"])
                self.assertTrue(engine.inspect_container(id_b)["State"]["Running"])
                self.assertNotIn("PortBindings", engine.inspect_container(id_a)["HostConfig"])
                reconciled = [item for item in engine.operations if item[0] == "exec_container"]
                self.assertTrue(any(item[1] == id_a for item in reconciled))
                self.assertTrue(any(item[1] == id_b for item in reconciled))

                for account_id, container_id in (("alpha", id_a), ("beta", id_b)):
                    labels = engine.inspect_container(container_id)["Config"]["Labels"]
                    self.assertEqual(labels["com.wechat-hub.account-id"], account_id)
                    self.assertEqual(labels["com.wechat-hub.provider"], "agent_wechat")

                manager.stop(account_a)
                self.assertFalse(engine.inspect_container(id_a)["State"]["Running"])
                self.assertTrue(engine.inspect_container(id_b)["State"]["Running"])

                account_a["agent_wechat"] = {"image": "ghcr.io/thisnick/agent-wechat:0.11.99"}
                recreated_a = manager.start(account_a)
                new_id_a = recreated_a["container_id"]
                self.assertNotEqual(new_id_a, id_a)
                self.assertEqual(
                    engine.inspect_container(new_id_a)["Config"]["Image"],
                    "ghcr.io/thisnick/agent-wechat:0.11.99",
                )
                self.assertEqual(engine.inspect_container(id_b)["Id"], id_b)

                manager.restart(account_b)
                self.assertTrue(engine.inspect_container(id_b)["State"]["Running"])

                a_data, a_home = manager.storage_names(account_a)
                b_data, b_home = manager.storage_names(account_b)
                manager.remove(account_a, purge_data=False)
                self.assertIn(a_data, engine.volumes)
                self.assertIn(a_home, engine.volumes)
                self.assertIn(b_data, engine.volumes)
                self.assertIn(b_home, engine.volumes)
                self.assertIsNotNone(engine.inspect_container(id_b))

                manager.start(account_a)
                manager.remove(account_a, purge_data=True)
                self.assertNotIn(a_data, engine.volumes)
                self.assertNotIn(a_home, engine.volumes)
                self.assertIn(b_data, engine.volumes)
                self.assertIn(b_home, engine.volumes)
                self.assertIsNotNone(engine.inspect_container(id_b))

    def test_agent_runtime_exports_upstream_verified_keys_over_private_adapter(self):
        class ExecDockerEngine:
            def __init__(self, rows):
                self.rows = rows

            def inspect_container(self, identifier):
                return {"State": {"Running": True}}

            def exec_container(self, identifier, command, *, env=None, timeout=30.0, attach_stderr=True):
                self.command = command
                self.env = env
                self.attach_stderr = attach_stderr
                return 0, b"ok\n" + json.dumps(self.rows).encode("utf-8")

        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            state_db = data / "agent.db"
            conn = sqlite3.connect(state_db)
            conn.execute(
                "CREATE TABLE wechat_keys ("
                "id TEXT PRIMARY KEY, session_id TEXT, account_dir TEXT, db_name TEXT, "
                "hex_key TEXT, verified_at TEXT)"
            )
            conn.execute(
                "INSERT INTO wechat_keys VALUES (?, ?, ?, ?, ?, ?)",
                ("1", "default", "wxid_personal", "session.db", "ab" * 32, "2026-09-01T01:00:00Z"),
            )
            conn.commit()
            conn.close()
            engine = ExecDockerEngine(
                [{"account_dir": "wxid_personal", "db_name": "session.db", "hex_key": "ab" * 32, "verified_at": "2026-09-01T01:00:00Z"}]
            )
            (root / "auth-token").write_text("a" * 64 + "\n", encoding="utf-8")
            manager = agent_wechat_runtime.AgentWechatManager(engine=engine)
            with patch.object(manager, "runtime_storage_root", return_value=root):
                result = manager.export_db_keys(account)
        self.assertIn('PRAGMA key = "$AGENT_TOKEN"', engine.command[2])
        self.assertIn("<<AGENT_SQL\n", engine.command[2])
        self.assertNotIn("<<'AGENT_SQL'", engine.command[2])
        self.assertTrue(engine.command[2].endswith("AGENT_SQL\n"))
        self.assertEqual(engine.env, [f"AGENT_TOKEN={'a' * 64}"])
        self.assertFalse(engine.attach_stderr)
        self.assertEqual(result["account_id"], "personal")
        self.assertEqual(result["runtime_provider"], "agent_wechat")
        self.assertEqual(result["credentials"][0]["account_dir"], "wxid_personal")
        self.assertEqual(result["credentials"][0]["db_name"], "session.db")
        self.assertEqual(result["credentials"][0]["hex_key"], "ab" * 32)

    def test_agent_full_login_flow_keeps_qr_in_memory_and_reaches_login_success(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        png = b"\x89PNG\r\n\x1a\nlogin-qr"
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

        class FakeWebSocket:
            def __init__(self):
                self.frames = iter(
                    [
                        json.dumps({"qr": {"qrDataUrl": data_url}}),
                        json.dumps({"phone_confirm": {"message": "confirm"}}),
                        json.dumps({"login_success": {"userId": "wxid_personal"}}),
                    ]
                )

            def recv(self):
                return next(self.frames)

            def close(self):
                return None

        class FakeWebsocketModule:
            @staticmethod
            def create_connection(url, timeout=0, enable_multithread=False):
                self.assertIn("/api/ws/login?", url)
                self.assertIn("token=", url)
                self.assertGreater(timeout, 0)
                self.assertTrue(enable_multithread)
                return FakeWebSocket()

        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": True,
            "state": "starting",
            "qr_data_url": "",
            "logged_in_user": "",
            "error": "",
            "updated_at": 0.0,
        }
        with patch.object(agent_wechat_runtime, "websocket", FakeWebsocketModule), patch.object(
            manager, "_token", return_value="a" * 64
        ):
            manager._run_login_flow(account, flow)

        self.assertEqual(flow["state"], "logged_in")
        self.assertEqual(flow["logged_in_user"], "wxid_personal")
        self.assertEqual(flow["qr_data_url"], "")
        self.assertFalse(flow["running"])

    def test_agent_full_login_flow_accepts_flat_upstream_event_envelope(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        png = b"\x89PNG\r\n\x1a\nlogin-qr"
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

        class FakeWebSocket:
            def __init__(self):
                self.frames = iter(
                    [
                        json.dumps({"type": "status", "message": "Waiting for scan"}),
                        json.dumps(
                            {
                                "type": "qr",
                                "qrData": "opaque-qr-identifier",
                                "qrDataUrl": data_url,
                            }
                        ),
                        json.dumps({"type": "login_success", "userId": "wxid_personal"}),
                    ]
                )

            def recv(self):
                return next(self.frames)

            def close(self):
                return None

        class FakeWebsocketModule:
            @staticmethod
            def create_connection(*_args, **_kwargs):
                return FakeWebSocket()

        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": True,
            "state": "starting",
            "qr_data_url": "",
            "logged_in_user": "",
            "error": "",
            "status_message": "",
            "updated_at": 0.0,
        }
        with patch.object(agent_wechat_runtime, "websocket", FakeWebsocketModule), patch.object(
            manager, "_token", return_value="a" * 64
        ):
            manager._run_login_flow(account, flow)

        self.assertEqual(flow["state"], "logged_in")
        self.assertEqual(flow["logged_in_user"], "wxid_personal")
        self.assertEqual(flow["status_message"], "Waiting for scan")
        self.assertEqual(flow["qr_data_url"], "")

    def test_agent_login_capture_is_nonblocking_until_start_produces_qr(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        healthy = {
            "running": True,
            "container_running": True,
            "agent_server_healthy": True,
            "runtime_health": "healthy",
            "wechat_login_status": "logged_out",
            "logged_in_user": "",
        }
        with patch.object(manager, "status", return_value=healthy):
            before = manager.capture_login(account)
        self.assertEqual(before["status"], "qr_not_ready")
        self.assertEqual(before["login_flow_state"], "idle")

    def test_live_logged_out_state_overrides_stale_login_flow_memory(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": False,
            "state": "logged_in",
            "qr_data_url": "",
            "logged_in_user": "old-wxid",
            "error": "",
            "updated_at": 0.0,
        }
        with agent_wechat_runtime._LOGIN_FLOWS_LOCK:
            agent_wechat_runtime._LOGIN_FLOWS["personal"] = flow
        try:
            with patch.object(
                manager,
                "status",
                return_value={
                    "running": True,
                    "container_running": True,
                    "agent_server_healthy": True,
                    "runtime_health": "healthy",
                    "wechat_login_status": "logged_out",
                    "logged_in_user": "",
                },
            ):
                status = manager.login_status(account)
            self.assertEqual(status["auth_status"], "logged_out")
            self.assertEqual(status["logged_in_user"], "")
        finally:
            agent_wechat_runtime._clear_login_flow("personal")
        self.assertNotIn("personal", agent_wechat_runtime._LOGIN_FLOWS)

    def test_visible_chat_does_not_finish_login_before_full_fsm_success(self):
        account = {"id": "personal", "display_name": "Personal", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        flow = {
            "lock": threading.Lock(),
            "thread": None,
            "running": True,
            "state": "phone_confirm",
            "qr_data_url": "",
            "logged_in_user": "",
            "error": "",
            "status_message": "Extracting database credentials",
            "updated_at": 0.0,
        }
        with agent_wechat_runtime._LOGIN_FLOWS_LOCK:
            agent_wechat_runtime._LOGIN_FLOWS["personal"] = flow
        try:
            with patch.object(
                manager,
                "status",
                return_value={
                    "running": True,
                    "container_running": True,
                    "agent_server_healthy": True,
                    "runtime_health": "healthy",
                    "wechat_login_status": "logged_in",
                    "logged_in_user": "wxid_personal",
                },
            ):
                status = manager.login_status(account)
            self.assertEqual(status["auth_status"], "unknown")
            self.assertEqual(status["logged_in_user"], "")
            self.assertEqual(status["login_flow_state"], "phone_confirm")
            self.assertEqual(status["login_flow_status"], "Extracting database credentials")
        finally:
            agent_wechat_runtime._clear_login_flow("personal")

    def test_bootstrap_replaces_stale_ready_marker_only_after_accounts_finish(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            ready = paths.runtime_dir / "bootstrap.ready"
            ready.write_text("stale\n", encoding="utf-8")

            def fake_bootstrap(account, runtime_paths):
                self.assertEqual(runtime_paths, paths)
                self.assertFalse(ready.exists())
                return False

            with patch.dict(
                os.environ,
                {"WECHAT_ACCOUNTS": "work", "WECHAT_LEGACY_DEFAULT_ACCOUNT": "false"},
                clear=False,
            ), patch.object(wechat_runtime, "require_root"), patch.object(
                wechat_runtime, "bootstrap_account", side_effect=fake_bootstrap
            ):
                wechat_runtime.bootstrap_all(registry)

            self.assertTrue(ready.exists())
            self.assertNotEqual(ready.read_text(encoding="utf-8"), "stale\n")

    def test_register_persists_human_name_and_control_routes_start(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            with patch.dict(
                os.environ,
                {"WECHAT_ACCOUNTS": "base", "WECHAT_LEGACY_DEFAULT_ACCOUNT": "false", "DISPLAY": ":1"},
                clear=False,
            ), patch.object(wechat_runtime, "require_root"), patch.object(
                wechat_runtime, "bootstrap_account", return_value=False
            ):
                registry.load(create=True)
                account = wechat_runtime.register_account(
                    registry,
                    "work",
                    ":2",
                    False,
                    "Work WeChat",
                )

            self.assertEqual(account["display_name"], "Work WeChat")
            self.assertEqual(account["display"], ":2")
            self.assertFalse(account["autostart"])
            persisted = wechat_runtime.find_account(registry.load(), "work")
            self.assertEqual(persisted["display_name"], "Work WeChat")

            with patch.object(
                wechat_runtime_control,
                "start_account",
                return_value={"account_id": "work", "running": True, "action": "started"},
            ) as start:
                result = wechat_runtime_control.dispatch_action(
                    registry,
                    {"action": "start", "account_id": "work"},
                )
            self.assertTrue(result["status"]["running"])
            start.assert_called_once()

    def test_login_status_and_snapshot_are_account_scoped_and_in_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = self.make_paths(Path(temp))
            registry = wechat_runtime.Registry(paths)
            with patch.dict(
                os.environ,
                {"WECHAT_ACCOUNTS": "work", "WECHAT_LEGACY_DEFAULT_ACCOUNT": "false", "DISPLAY": ":1"},
                clear=False,
            ), patch.object(wechat_runtime, "require_root"), patch.object(
                wechat_runtime, "bootstrap_account", return_value=False
            ):
                data = registry.load(create=True)
            account = wechat_runtime.find_account(data, "work")
            status = {
                "account_id": "work",
                "display_name": "work",
                "running": True,
                "pids": [321],
                "windows": [{"window_id": 99, "pid": 321, "title": "Weixin"}],
            }
            png = b"\x89PNG\r\n\x1a\nmock-png"
            with patch.object(wechat_runtime_control, "status_for", return_value=status):
                login = wechat_runtime_control.dispatch_action(
                    registry, {"action": "login_status", "account_id": "work"}
                )["login"]
                self.assertTrue(login["snapshot_available"])
                self.assertEqual(login["window_id"], 99)
                with patch.object(wechat_runtime_control, "user_exec_prefix", return_value=[]), patch.object(
                    wechat_runtime_control.subprocess, "check_output", return_value=png
                ) as capture:
                    snapshot = wechat_runtime_control.dispatch_action(
                        registry, {"action": "capture_login", "account_id": "work"}
                    )
            self.assertEqual(base64.b64decode(snapshot["content_base64"]), png)
            self.assertEqual(snapshot["content_type"], "image/png")
            self.assertIn("--window-id", capture.call_args.args[0])

    def test_account_list_probes_in_parallel_after_releasing_control_lock(self):
        class ListRegistry:
            def load(self, create=False):
                del create
                return {
                    "accounts": [
                        {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"},
                        {"id": "beta", "display_name": "Beta", "runtime_provider": "agent_wechat"},
                    ]
                }

        both_entered = threading.Event()
        entered = []
        entered_lock = threading.Lock()

        def fake_status(account):
            with entered_lock:
                entered.append(account["id"])
                if len(entered) == 2:
                    both_entered.set()
            self.assertTrue(both_entered.wait(1.0), "account probes were serialized")
            acquired = wechat_runtime_control.CONTROL_LOCK.acquire(timeout=0.5)
            try:
                self.assertTrue(acquired, "global CONTROL_LOCK remained held during network probe")
            finally:
                if acquired:
                    wechat_runtime_control.CONTROL_LOCK.release()
            return {
                "account_id": account["id"],
                "runtime_health": "healthy",
                "agent_server_healthy": True,
            }

        with patch.object(wechat_runtime_control, "_list_status_for", side_effect=fake_status):
            result = wechat_runtime_control.dispatch_action(ListRegistry(), {"action": "list"})
        self.assertEqual([item["account_id"] for item in result["accounts"]], ["alpha", "beta"])
        self.assertTrue(all(item["runtime_health"] == "healthy" for item in result["accounts"]))

    def test_account_list_returns_healthy_peer_when_one_probe_degrades(self):
        accounts = [
            {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"},
            {"id": "beta", "display_name": "Beta", "runtime_provider": "agent_wechat"},
        ]

        def fake_status(account):
            if account["id"] == "beta":
                raise RuntimeError("agent health timeout")
            return {
                "account_id": "alpha",
                "runtime_provider": "agent_wechat",
                "runtime_health": "healthy",
                "agent_server_healthy": True,
            }

        with patch.object(wechat_runtime_control, "_list_status_for", side_effect=fake_status):
            rows = wechat_runtime_control.list_account_statuses(accounts)
        by_id = {item["account_id"]: item for item in rows}
        self.assertEqual(by_id["alpha"]["runtime_health"], "healthy")
        self.assertEqual(by_id["beta"]["runtime_health"], "degraded")
        self.assertFalse(by_id["beta"]["agent_server_healthy"])
        self.assertIn("timeout", by_id["beta"]["health_error"])

    def test_list_agent_status_uses_short_probe_timeout(self):
        account = {"id": "alpha", "display_name": "Alpha", "runtime_provider": "agent_wechat"}
        expected = {"account_id": "alpha", "runtime_health": "healthy"}
        with patch.object(agent_wechat_runtime.AgentWechatManager, "status", return_value=expected) as status:
            result = wechat_runtime_control._list_status_for(account)
        self.assertEqual(result, expected)
        status.assert_called_once_with(account, probe_timeout=wechat_runtime_control.LIST_AGENT_PROBE_TIMEOUT)

    def test_selkies_clipboard_cannot_be_reenabled_by_env_in_rc2(self):
        with patch.dict(os.environ, {"WECHAT_SELKIES_CLIPBOARD_ENABLED": "true"}, clear=False):
            features = agent_wechat_runtime.selkies_desktop_features()
            self.assertFalse(features["clipboard_text"])
            self.assertFalse(features["clipboard_image"])
            env = set(agent_wechat_runtime._selkies_attach_env())
            self.assertIn("SELKIES_CLIPBOARD_ENABLED=false|locked", env)
            self.assertIn("SELKIES_ENABLE_BINARY_CLIPBOARD=false|locked", env)
            self.assertIn("SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD=false|locked", env)

        with patch.dict(os.environ, {"WECHAT_SELKIES_CLIPBOARD_ENABLED": "false"}, clear=False):
            features = agent_wechat_runtime.selkies_desktop_features()
            self.assertFalse(features["clipboard_text"])
            self.assertFalse(features["clipboard_image"])
            env = set(agent_wechat_runtime._selkies_attach_env())
            self.assertIn("SELKIES_CLIPBOARD_ENABLED=false|locked", env)
            self.assertIn("SELKIES_ENABLE_BINARY_CLIPBOARD=false|locked", env)
            self.assertIn("SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD=false|locked", env)

    def test_runtime_manager_baseimage_clipboard_is_hard_disabled_and_pids_bounded(self):
        runtime_root = Path(__file__).resolve().parents[1]
        dockerfile = (runtime_root / "Dockerfile").read_text(encoding="utf-8")
        compose = (runtime_root / "docker-compose.yml").read_text(encoding="utf-8")

        for setting in (
            'SELKIES_CLIPBOARD_ENABLED="false|locked"',
            'SELKIES_CLIPBOARD_IN_ENABLED="false|locked"',
            'SELKIES_CLIPBOARD_OUT_ENABLED="false|locked"',
            'SELKIES_ENABLE_BINARY_CLIPBOARD="false|locked"',
            'SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD="false|locked"',
        ):
            self.assertIn(setting, dockerfile)

        for setting in (
            "SELKIES_CLIPBOARD_ENABLED=false|locked",
            "SELKIES_CLIPBOARD_IN_ENABLED=false|locked",
            "SELKIES_CLIPBOARD_OUT_ENABLED=false|locked",
            "SELKIES_ENABLE_BINARY_CLIPBOARD=false|locked",
            "SELKIES_UI_SIDEBAR_SHOW_CLIPBOARD=false|locked",
        ):
            self.assertIn(setting, compose)
        self.assertIn("pids_limit: 200", compose)

    def test_companion_pids_limit_and_resource_caps_override(self):
        account = {"id": "beta", "display_name": "Beta", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())
        with patch.dict(
            os.environ,
            {
                "WECHAT_SELKIES_PIDS_LIMIT": "50",
                "WECHAT_SELKIES_MEM_LIMIT_MB": "512",
                "WECHAT_SELKIES_CPU_LIMIT_CORES": "1.5",
            },
            clear=False,
        ), patch.object(manager, "selkies_image_for", return_value="sha256:img"):
            payload = manager._selkies_payload(
                account,
                parent_container_id="agent-beta-id",
                x11_volume="beta-x11",
                browser_files_volume="beta-files",
                host_desktop_token="/host/token",
            )
        host = payload["HostConfig"]
        self.assertEqual(host["PidsLimit"], 50)
        self.assertEqual(host["Memory"], 512 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 1500000000)

    def test_safety_resource_overrides_cannot_disable_or_unbound_limits(self):
        account = {"id": "bounded", "display_name": "Bounded", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=object())

        with patch.dict(
            os.environ,
            {
                "WECHAT_SELKIES_PIDS_LIMIT": "-1",
                "WECHAT_SELKIES_MEM_LIMIT_MB": "0",
                "WECHAT_SELKIES_CPU_LIMIT_CORES": "999",
                "AGENT_WECHAT_PIDS_LIMIT": "9999999",
                "AGENT_WECHAT_MEM_LIMIT_MB": "0",
            },
            clear=False,
        ), patch.object(manager, "selkies_image_for", return_value="sha256:img"):
            companion = manager._selkies_payload(
                account,
                parent_container_id="agent-bounded-id",
                x11_volume="bounded-x11",
                browser_files_volume="bounded-files",
                host_desktop_token="/host/token",
            )
            primary = manager._container_payload(
                account,
                network="hub-net",
                data_volume="bounded-data",
                home_volume="bounded-home",
                host_token="/host/auth-token",
            )

        companion_host = companion["HostConfig"]
        primary_host = primary["HostConfig"]
        # Non-positive values fall back to safe defaults rather than Docker's
        # unlimited semantics. Excessively high overrides are clamped.
        self.assertEqual(companion_host["PidsLimit"], 100)
        self.assertEqual(companion_host["Memory"], 1024 * 1024 * 1024)
        self.assertEqual(companion_host["NanoCpus"], 4000000000)
        self.assertEqual(primary_host["PidsLimit"], 1024)
        self.assertEqual(primary_host["Memory"], 2048 * 1024 * 1024)

    def test_desktop_session_release_cleans_up_companion_container(self):
        import unittest.mock
        removed_accounts = []
        mock_manager = unittest.mock.MagicMock()
        mock_manager._remove_selkies_container = lambda acc: removed_accounts.append(acc["id"])
        with patch("desktop_gateway.AgentWechatManager", return_value=mock_manager), patch.dict(
            os.environ, {"WECHAT_SELKIES_IDLE_TTL_SECONDS": "0"}, clear=False
        ):
            self.assertTrue(desktop_gateway.acquire_manual_gui_lease("test-acc", "sess-1"))
            desktop_gateway.release_manual_gui_lease("test-acc", "sess-1")
            self.assertIn("test-acc", removed_accounts)

    def test_ensure_selkies_desktop_cleans_up_on_probe_failure(self):
        engine = FakeDockerEngine()
        manager = agent_wechat_runtime.AgentWechatManager(engine=engine)
        account = {"id": "acc-fail", "display_name": "Fail", "runtime_provider": "agent_wechat"}
        primary_id = engine.create_container(
            "wechat-agent-acc-fail",
            {
                "Image": "ghcr.io/thisnick/agent-wechat:0.11.15",
                "Labels": agent_wechat_runtime._labels("acc-fail"),
                "HostConfig": {
                    "Mounts": [
                        {"Target": "/tmp/.X11-unix"},
                        {"Target": "/home/wechat/WeChatHubFiles"},
                    ]
                },
            },
        )["Id"]
        engine.start_container(primary_id)
        with patch.object(
            manager, "_host_config_root_and_network", return_value=("/host/root", "wechat-hub_net")
        ), patch.object(
            manager, "_ensure_desktop_volumes", return_value=("x11-vol", "files-vol")
        ), patch.object(
            manager, "_desktop_token_host_path", return_value="/host/token"
        ), patch.object(
            manager, "selkies_image_for", return_value="sha256:rt-image"
        ), patch.object(
            manager, "_probe_selkies", return_value=(False, "Connection refused")
        ), patch("time.monotonic", side_effect=[0.0, 1.0, 20.0]):
            with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
                manager.ensure_selkies_desktop(account)
            # Verify companion container was removed after failure
            self.assertEqual(engine.managed_containers("acc-fail", provider="agent_wechat_selkies"), [])


    def test_companion_failure_on_a_does_not_affect_b_desktop(self):
        engine = FakeDockerEngine()
        manager = agent_wechat_runtime.AgentWechatManager(engine=engine)
        account_a = {"id": "acc-a", "display_name": "A", "runtime_provider": "agent_wechat"}
        account_b = {"id": "acc-b", "display_name": "B", "runtime_provider": "agent_wechat"}

        for acc in (account_a, account_b):
            pid = engine.create_container(
                f"wechat-agent-{acc['id']}",
                {
                    "Image": "ghcr.io/thisnick/agent-wechat:0.11.15",
                    "Labels": agent_wechat_runtime._labels(acc["id"]),
                    "HostConfig": {
                        "Mounts": [
                            {"Target": "/tmp/.X11-unix"},
                            {"Target": "/home/wechat/WeChatHubFiles"},
                        ]
                    },
                },
            )["Id"]
            engine.start_container(pid)

        def fake_probe(acc, timeout=1.0):
            if acc["id"] == "acc-a":
                return (False, "A companion PIDs limit / fork failure")
            return (True, "")

        with patch.object(
            manager, "_host_config_root_and_network", return_value=("/host/root", "wechat-hub_net")
        ), patch.object(
            manager, "_ensure_desktop_volumes", return_value=("x11-vol", "files-vol")
        ), patch.object(
            manager, "_desktop_token_host_path", return_value="/host/token"
        ), patch.object(
            manager, "selkies_image_for", return_value="sha256:rt-image"
        ), patch.object(
            manager, "_probe_selkies", side_effect=fake_probe
        ), patch("time.monotonic", side_effect=[0.0, 1.0, 20.0, 0.0, 1.0]):
            with self.assertRaises(agent_wechat_runtime.AgentWechatRuntimeError):
                manager.ensure_selkies_desktop(account_a)
            self.assertEqual(engine.managed_containers("acc-a", provider="agent_wechat_selkies"), [])

            res_b = manager.ensure_selkies_desktop(account_b)
            self.assertEqual(res_b["account_id"], "acc-b")
            self.assertEqual(res_b["desktop_provider"], "selkies")
            self.assertEqual(len(engine.managed_containers("acc-b", provider="agent_wechat_selkies")), 1)

    def test_runtime_account_api_returns_fast_degraded_when_companion_fails(self):
        account = {"id": "acc-degraded", "display_name": "Degraded", "runtime_provider": "agent_wechat"}
        manager = agent_wechat_runtime.AgentWechatManager(engine=FakeDockerEngine())
        inspected = {
            "Id": "fake-degraded",
            "Name": "/wechat-agent-acc-degraded",
            "Config": {"Image": "ghcr.io/thisnick/agent-wechat:0.11.15"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {}},
        }
        with patch.object(manager, "_find_container", return_value=inspected):
            with patch.object(manager, "_probe_agent_server", return_value=(False, "PIDs limit reached: fork rejected")):
                status = manager.status(account, probe_timeout=1.0)
                self.assertEqual(status["runtime_health"], "degraded")
                self.assertFalse(status["agent_server_healthy"])
                self.assertIn("PIDs limit", status["health_error"])

    def test_repeated_session_acquire_release_has_bounded_idle_reap(self):
        removed = []
        mock_manager = unittest.mock.MagicMock()
        mock_manager._remove_selkies_container = lambda acc: removed.append(acc["id"])

        with patch("desktop_gateway.AgentWechatManager", return_value=mock_manager), patch.dict(
            os.environ, {"WECHAT_SELKIES_IDLE_TTL_SECONDS": "0"}, clear=False
        ):
            for i in range(50):
                sess = f"sess-{i}"
                self.assertTrue(desktop_gateway.acquire_manual_gui_lease("acc-churn", sess))
                desktop_gateway.release_manual_gui_lease("acc-churn", sess)

            with desktop_gateway._MANUAL_GUI_LEASES_GUARD:
                self.assertNotIn("acc-churn", desktop_gateway._MANUAL_GUI_LEASES)
            self.assertEqual(len(desktop_gateway._IDLE_CLEANUP_TIMERS), 0)
            self.assertEqual(len(removed), 50)


if __name__ == "__main__":
    unittest.main()
