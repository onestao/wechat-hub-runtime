"""Consumer Control tests: Disabled / EFB / Agent with strict mutual exclusion.

Console must never touch the Docker socket, so every consumer operation goes
through the Runtime control plane.  These tests pin the safety properties:

* EFB and Agent are never RUNNING at the same time;
* a switch stops the outgoing consumer and *confirms* it stopped before the
  incoming one is started, and aborts if it cannot confirm;
* an unconfigured EFB cannot be started into a crash loop;
* the Telegram token is never part of any response.
"""

import importlib.util
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1] / "root" / "scripts" / "wechat"


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


agent_wechat_runtime = _load_module("agent_wechat_runtime", MODULE_DIR / "agent_wechat_runtime.py")
consumer_control = _load_module("consumer_control", MODULE_DIR / "consumer_control.py")


EFB_IMAGE = "ghcr.io/onestao/wechat-hub-efb-linux-wechat-slave@sha256:" + "a" * 64
AGENT_IMAGE = "ghcr.io/onestao/wechat-hub-agent@sha256:" + "b" * 64


class FakeEngine:
    def __init__(self, *, images=(), containers=None):
        self.containers = dict(containers or {})
        self.images = set(images)
        self.operations = []
        self.created: dict[str, dict] = {}
        self.stop_effective = True

    @property
    def available(self):
        return True

    def inspect_container(self, identifier):
        return self.containers.get(identifier)

    def request(self, method, path, payload=None, **kwargs):
        del payload, kwargs
        prefix = "/images/"
        if method == "GET" and path.startswith(prefix) and path.endswith("/json"):
            ref = urllib.parse.unquote(path[len(prefix):-len("/json")])
            if ref in self.images:
                return {}
            raise agent_wechat_runtime.AgentWechatRuntimeError(
                f"Docker Engine GET {path} returned 404: no such image"
            )
        raise AssertionError(f"unexpected request: {method} {path}")

    def _register(self, container):
        """Containers are addressable by name and by id, like Docker."""
        self.containers[container["Id"]] = container
        return container

    def create_container(self, name, payload):
        self.operations.append(("create", name))
        self.created[name] = payload
        container = self._register(
            {
                "Id": f"id-{name}",
                "Config": {"Image": payload["Image"], "Labels": dict(payload.get("Labels") or {})},
                "State": {
                    "Running": False,
                    "ExitCode": 0,
                    "StartedAt": "",
                    "FinishedAt": "",
                    "Error": "",
                },
                "RestartCount": 0,
                "Mounts": [],
                "NetworkSettings": {"Networks": {}},
            }
        )
        self.containers[name] = container
        return container

    def start_container(self, identifier):
        self.operations.append(("start", identifier))
        self.containers[identifier]["State"]["Running"] = True

    def stop_container(self, identifier, timeout=10):
        del timeout
        self.operations.append(("stop", identifier))
        if self.stop_effective:
            self.containers[identifier]["State"]["Running"] = False

    def _container(self, consumer, *, running):
        name = consumer_control.CONSUMERS[consumer]["container_name"]
        image = EFB_IMAGE if consumer == consumer_control.CONSUMER_EFB else AGENT_IMAGE
        return {
            "Id": f"id-{name}",
            "Config": {
                "Image": image,
                "Labels": {consumer_control.CONSUMER_LABEL: consumer},
            },
            "State": {"Running": running, "ExitCode": 0, "StartedAt": "", "FinishedAt": "", "Error": ""},
            "RestartCount": 0,
            "Mounts": [],
            "NetworkSettings": {"Networks": {}},
        }

    def add(self, consumer, *, running):
        name = consumer_control.CONSUMERS[consumer]["container_name"]
        container = self._container(consumer, running=running)
        self._register(container)
        self.containers[name] = container

    def running(self):
        return sorted(
            consumer
            for consumer in consumer_control.CONSUMERS
            if self.containers.get(
                consumer_control.CONSUMERS[consumer]["container_name"], {}
            ).get("State", {}).get("Running")
        )


class ConsumerControlTests(unittest.TestCase):
    def make_control(self, engine, profile_root: Path):
        control = consumer_control.ConsumerControl(engine=engine)
        patcher = patch.object(control, "_host_config_root", return_value="/config"), patch.object(
            control, "_network", return_value="wechat-hub-internal"
        )
        return control, patcher

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.profile_root = Path(self._temp.name) / "efb-profile"
        (self.profile_root / "profiles" / "default").mkdir(parents=True, exist_ok=True)
        self._env = patch.dict(
            "os.environ",
            {
                "EFB_IMAGE": EFB_IMAGE,
                "AGENT_IMAGE": AGENT_IMAGE,
                "EFB_PROFILE_DIR": str(self.profile_root),
                "WECHAT_RUNTIME_DIR": str(Path(self._temp.name) / "state"),
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self._sleep = patch.object(consumer_control.time, "sleep", return_value=None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _configure_efb(self, *, token=True):
        default = self.profile_root / "profiles" / "default"
        (default / "config.yaml").write_text(
            "master_channel: blueset.telegram\nslave_channels:\n  - wechat.linux\nmiddlewares: []\n",
            encoding="utf-8",
        )
        module = default / "blueset.telegram"
        module.mkdir(parents=True, exist_ok=True)
        body = "token: 123456:SUPER-SECRET-TOKEN\nadmins:\n  - 407680985\n" if token else "admins:\n  - 1\n"
        (module / "config.yaml").write_text(body, encoding="utf-8")

    # -- configuration ----------------------------------------------------

    def test_efb_unconfigured_cannot_start(self):
        engine = FakeEngine(images=(EFB_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            status = control.snapshot()
            self.assertFalse(status["consumers"]["efb"]["configured"])
            self.assertFalse(status["consumers"]["efb"]["can_start"])
            self.assertIn("Telegram", status["consumers"]["efb"]["blocked_reason"])
            with self.assertRaises(consumer_control.ConsumerControlError) as ctx:
                control.start(consumer_control.CONSUMER_EFB)
        self.assertEqual(ctx.exception.code, "consumer_not_configured")
        self.assertEqual(engine.operations, [])

    def test_efb_configured_detected_without_leaking_token(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            status = control.snapshot()
        efb = status["consumers"]["efb"]
        self.assertTrue(efb["configured"])
        self.assertTrue(efb["can_start"])
        serialized = repr(status)
        self.assertNotIn("SUPER-SECRET-TOKEN", serialized)
        self.assertNotIn("123456", serialized)

    def test_efb_master_channel_without_token_is_not_configured(self):
        self._configure_efb(token=False)
        engine = FakeEngine(images=(EFB_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            efb = control.snapshot()["consumers"]["efb"]
        self.assertFalse(efb["configured"])
        self.assertIn("token", efb["configuration_detail"])

    def test_agent_needs_no_configuration(self):
        engine = FakeEngine(images=(AGENT_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            agent = control.snapshot()["consumers"]["agent"]
        self.assertTrue(agent["configured"])
        self.assertTrue(agent["can_start"])

    # -- mutual exclusion --------------------------------------------------

    def test_switch_efb_to_agent_stops_before_starting(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE, AGENT_IMAGE))
        engine.add(consumer_control.CONSUMER_EFB, running=True)
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            result = control.set_mode(consumer_control.MODE_AGENT)
        self.assertEqual(result["mode"], consumer_control.MODE_AGENT)
        self.assertEqual(engine.running(), [consumer_control.CONSUMER_AGENT])
        stop_index = engine.operations.index(("stop", "id-wechat-hub-efb"))
        start_index = engine.operations.index(("start", "id-wechat-hub-agent"))
        self.assertLess(stop_index, start_index)

    def test_switch_agent_to_efb_stops_before_starting(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE, AGENT_IMAGE))
        engine.add(consumer_control.CONSUMER_AGENT, running=True)
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            result = control.set_mode(consumer_control.MODE_EFB)
        self.assertEqual(result["mode"], consumer_control.MODE_EFB)
        self.assertEqual(engine.running(), [consumer_control.CONSUMER_EFB])
        stop_index = engine.operations.index(("stop", "id-wechat-hub-agent"))
        start_index = engine.operations.index(("start", "id-wechat-hub-efb"))
        self.assertLess(stop_index, start_index)

    def test_switch_to_disabled_stops_everything(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE, AGENT_IMAGE))
        engine.add(consumer_control.CONSUMER_EFB, running=True)
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            result = control.set_mode(consumer_control.MODE_DISABLED)
        self.assertEqual(result["mode"], consumer_control.MODE_DISABLED)
        self.assertEqual(engine.running(), [])

    def test_unconfirmable_stop_aborts_the_switch(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE, AGENT_IMAGE))
        engine.add(consumer_control.CONSUMER_EFB, running=True)
        engine.stop_effective = False
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1], patch.object(
            consumer_control, "STOP_CONFIRM_ATTEMPTS", 3
        ):
            with self.assertRaises(consumer_control.ConsumerControlError) as ctx:
                control.set_mode(consumer_control.MODE_AGENT)
        self.assertEqual(ctx.exception.code, "consumer_stop_timeout")
        self.assertNotIn(("start", "id-wechat-hub-agent"), engine.operations)
        self.assertEqual(engine.running(), [consumer_control.CONSUMER_EFB])

    def test_only_one_consumer_can_ever_be_running(self):
        self._configure_efb()
        engine = FakeEngine(images=(EFB_IMAGE, AGENT_IMAGE))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            control.set_mode(consumer_control.MODE_EFB)
            self.assertEqual(len(engine.running()), 1)
            control.set_mode(consumer_control.MODE_AGENT)
            self.assertEqual(engine.running(), [consumer_control.CONSUMER_AGENT])
            control.set_mode(consumer_control.MODE_DISABLED)
            self.assertEqual(engine.running(), [])

    # -- provisioning safety ----------------------------------------------

    def test_missing_image_blocks_start_with_actionable_reason(self):
        engine = FakeEngine(images=())
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            agent = control.snapshot()["consumers"]["agent"]
            self.assertFalse(agent["image_present"])
            self.assertFalse(agent["can_start"])
            with self.assertRaises(consumer_control.ConsumerControlError) as ctx:
                control.start(consumer_control.CONSUMER_AGENT)
        self.assertEqual(ctx.exception.code, "consumer_image_unavailable")

    def test_foreign_container_with_reserved_name_is_never_touched(self):
        engine = FakeEngine(images=(AGENT_IMAGE,))
        engine.containers["wechat-hub-agent"] = {
            "Id": "id-foreign",
            "Config": {"Image": AGENT_IMAGE, "Labels": {}},
            "State": {"Running": True, "ExitCode": 0, "StartedAt": "", "FinishedAt": "", "Error": ""},
            "RestartCount": 0,
            "Mounts": [],
            "NetworkSettings": {"Networks": {}},
        }
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            agent = control.snapshot()["consumers"]["agent"]
            self.assertFalse(agent["provisioned"])
            control.set_mode(consumer_control.MODE_DISABLED)
        self.assertEqual(engine.operations, [])
        self.assertEqual(engine.running(), [consumer_control.CONSUMER_AGENT])

    def test_failed_consumer_reports_failed_state(self):
        engine = FakeEngine(images=(AGENT_IMAGE,))
        engine.add(consumer_control.CONSUMER_AGENT, running=False)
        engine.containers["wechat-hub-agent"]["State"]["ExitCode"] = 137
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            agent = control.snapshot()["consumers"]["agent"]
        self.assertEqual(agent["state"], "failed")
        self.assertEqual(agent["exit_code"], 137)

    # -- container shape: network + durable state --------------------------

    def test_agent_joins_the_consumer_network_by_dns_name(self):
        """The Agent is addressed as http://wechat-hub-agent:8091 over Docker DNS.

        No address is ever resolved, cached or exposed: the container joins the
        shared internal network under its own fixed name, which is all the
        Console needs.
        """

        engine = FakeEngine(images=(AGENT_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            control.start(consumer_control.CONSUMER_AGENT)

        payload = engine.created["wechat-hub-agent"]
        self.assertEqual(payload["HostConfig"]["NetworkMode"], "wechat-hub-internal")
        self.assertEqual(payload["Image"], AGENT_IMAGE)
        self.assertEqual(payload["Labels"][consumer_control.CONSUMER_LABEL], consumer_control.CONSUMER_AGENT)
        self.assertIn("/config/agent-data:/data", payload["HostConfig"]["Binds"])

    def test_agent_container_carries_core_url_and_durable_db_path(self):
        engine = FakeEngine(images=(AGENT_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            control.start(consumer_control.CONSUMER_AGENT)

        env = engine.created["wechat-hub-agent"]["Env"]
        self.assertIn("WECHAT_CORE_URL=http://wechat-hub-core:8080", env)
        # The Runtime recreates this container on every mode switch, so the
        # consumer's own database must live on the persistent /data bind.
        self.assertIn("WECHAT_AGENT_DB=/data/agent.sqlite", env)

    def test_efb_uses_the_unified_runtime_config_profile_path(self):
        self._configure_efb(token=True)
        engine = FakeEngine(images=(EFB_IMAGE,))
        control, patchers = self.make_control(engine, self.profile_root)
        with patchers[0], patchers[1]:
            control.start(consumer_control.CONSUMER_EFB)

        payload = engine.created["wechat-hub-efb"]
        self.assertEqual(payload["HostConfig"]["NetworkMode"], "wechat-hub-internal")
        self.assertIn("/config/efb-profile:/root/.ehforwarderbot", payload["HostConfig"]["Binds"])
        # EFB reads its configuration from the mounted profile, not from env.
        self.assertEqual(payload["Env"], [])

    def test_default_images_are_immutable_digests(self):
        """A floating tag would silently change what a consumer runs."""

        for consumer, spec in consumer_control.CONSUMERS.items():
            with self.subTest(consumer=consumer):
                self.assertIn("@sha256:", str(spec["default_image"]))
                self.assertNotIn(":latest", str(spec["default_image"]))

    def test_consumer_network_env_overrides_the_agent_wechat_network(self):
        engine = FakeEngine(images=(AGENT_IMAGE,))
        control = consumer_control.ConsumerControl(engine=engine)
        with patch.dict(
            "os.environ",
            {"CONSUMER_NETWORK": "wechat-hub-internal", "AGENT_WECHAT_NETWORK": "something-else"},
            clear=False,
        ):
            self.assertEqual(control._network(), "wechat-hub-internal")


if __name__ == "__main__":
    unittest.main()
