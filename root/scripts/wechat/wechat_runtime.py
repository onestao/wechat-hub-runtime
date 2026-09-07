#!/usr/bin/env python3
"""Account-aware process manager for the official Linux WeChat client.

This module intentionally lives inside the upstream wechat-selkies script tree.
It replaces the original global pgrep/pkill process model with an account
registry, Unix-user isolation, account-specific HOME/XDG paths, PID discovery,
window discovery and health/status output.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux runtime dependency
    fcntl = None

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - lets pure registry tests run on Windows
    grp = None
    pwd = None


REGISTRY_VERSION = 2
INSTANCE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_UID_BASE = 20000
DEFAULT_DISPLAY = ":1"
DEVICE_GROUP_ALLOWLIST = {"audio", "input", "plugdev", "render", "video"}
RUNTIME_PROVIDERS = {"legacy", "agent_wechat"}


class RuntimeErrorWithHint(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    registry_file: Path
    account_home_root: Path
    runtime_dir: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        return cls(
            registry_file=Path(
                os.environ.get(
                    "WECHAT_ACCOUNTS_FILE", "/config/wechat-runtime/accounts.json"
                )
            ),
            account_home_root=Path(
                os.environ.get(
                    "WECHAT_ACCOUNT_HOME_ROOT", "/config/wechat-accounts"
                )
            ),
            runtime_dir=Path(
                os.environ.get("WECHAT_RUNTIME_DIR", "/run/wechat-runtime")
            ),
        )


def bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_account_id(account_id: str) -> str:
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError(
            "account id must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
        )
    return account_id


def validate_instance_uuid(instance_uuid: str) -> str:
    raw = str(instance_uuid).strip()
    try:
        parsed = uuid.UUID(raw)
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"invalid instance_uuid: {instance_uuid!r}")


def validate_runtime_alias(runtime_alias: str) -> str:
    return validate_account_id(runtime_alias)


def account_username(account_id: str) -> str:
    validate_account_id(account_id)
    normalized = re.sub(r"[^a-z0-9_]", "_", account_id.lower())
    if not normalized or not normalized[0].isalpha():
        normalized = f"a_{normalized}"
    base = f"wx_{normalized}"
    if len(base) <= 28:
        return base
    digest = hashlib.sha1(account_id.encode("utf-8")).hexdigest()[:6]
    return f"{base[:21]}_{digest}"


def sanitize_account_runtime_name(account_id: str) -> str:
    """Return a Docker-safe, collision-resistant account resource suffix."""

    validate_account_id(account_id)
    normalized = re.sub(r"[^a-z0-9]+", "-", account_id.lower()).strip("-")
    normalized = (normalized or "account")[:32].rstrip("-") or "account"
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:8]
    return f"{normalized}-{digest}"


def derive_legacy_resource_key(account: Dict[str, Any]) -> str:
    """Extract existing resource suffix or derive a stable Docker-safe key."""
    aw = account.get("agent_wechat")
    if isinstance(aw, dict):
        cname = str(aw.get("container_name") or "")
        if cname.startswith("wechat-agent-"):
            return cname.removeprefix("wechat-agent-")
        dvol = str(aw.get("data_volume") or "")
        if dvol.startswith("wechat-agent-") and dvol.endswith("-data"):
            return dvol.removeprefix("wechat-agent-").removesuffix("-data")
    target = str(account.get("runtime_alias") or account.get("id") or "account")
    return sanitize_account_runtime_name(target)


def runtime_provider(account: Dict[str, Any]) -> str:
    provider = str(account.get("runtime_provider") or "legacy").strip().lower()
    if provider not in RUNTIME_PROVIDERS:
        raise RuntimeErrorWithHint(
            f"unsupported runtime_provider {provider!r}; expected one of {sorted(RUNTIME_PROVIDERS)}"
        )
    return provider


def display_lock_name(display_name: str) -> str:
    value = display_name or DEFAULT_DISPLAY
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return safe or "display"


def parse_account_ids(raw: Optional[str]) -> List[str]:
    if raw is None or not raw.strip():
        return [DEFAULT_ACCOUNT_ID]
    result: List[str] = []
    seen = set()
    for item in raw.split(","):
        account_id = validate_account_id(item.strip())
        if account_id not in seen:
            seen.add(account_id)
            result.append(account_id)
    if not result:
        return [DEFAULT_ACCOUNT_ID]
    return result


def parse_display_map(raw: Optional[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not raw:
        return result
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "WECHAT_ACCOUNT_DISPLAY_MAP entries must be account=:display"
            )
        account_id, display_name = item.split("=", 1)
        account_id = validate_account_id(account_id.strip())
        display_name = display_name.strip()
        if not display_name:
            raise ValueError(f"empty display for account {account_id}")
        result[account_id] = display_name
    return result


class Registry:
    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        self._lock_depth = 0
        self._lock_handle = None
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self.paths.runtime_dir / "registry.lock"
            if self._lock_depth == 0:
                self._lock_handle = lock_path.open("a+", encoding="utf-8")
                if fcntl is not None:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    try:
                        if fcntl is not None and self._lock_handle is not None:
                            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        if self._lock_handle is not None:
                            self._lock_handle.close()
                            self._lock_handle = None

    def load(self, create: bool = False) -> Dict[str, Any]:
        if self.paths.registry_file.exists():
            data = json.loads(self.paths.registry_file.read_text(encoding="utf-8"))
            if self._needs_migration(data):
                with self.locked():
                    # Re-read inside lock in case another process already migrated
                    data = json.loads(self.paths.registry_file.read_text(encoding="utf-8"))
                    if self._needs_migration(data):
                        data = self._migrate_registry_data(data)
                        self.save(data)
            self._validate(data)
            return data
        if not create:
            raise RuntimeErrorWithHint(
                f"registry does not exist: {self.paths.registry_file}; run bootstrap"
            )
        data = self._initial_registry()
        self.save(data)
        return data

    def _needs_migration(self, data: Dict[str, Any]) -> bool:
        if data.get("version") != REGISTRY_VERSION:
            return True
        accounts = data.get("accounts")
        if not isinstance(accounts, list):
            return False
        for acc in accounts:
            if not isinstance(acc, dict):
                return False
            if not acc.get("instance_uuid"):
                return True
            if not acc.get("runtime_alias"):
                return True
            if not acc.get("resource_key"):
                return True
            if not acc.get("display_name"):
                return True
            if "id" not in acc:
                return True
        return False

    def _migrate_registry_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        new_data = dict(data)
        new_data["version"] = REGISTRY_VERSION
        accounts = new_data.get("accounts")
        if not isinstance(accounts, list):
            return new_data

        seen_uuids = set()
        for acc in accounts:
            if isinstance(acc, dict) and acc.get("instance_uuid"):
                seen_uuids.add(str(acc["instance_uuid"]).lower())

        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            # 1. instance_uuid: generate once under lock if missing
            if not acc.get("instance_uuid"):
                while True:
                    candidate = str(uuid.uuid4())
                    if candidate.lower() not in seen_uuids:
                        acc["instance_uuid"] = candidate
                        seen_uuids.add(candidate.lower())
                        break

            # 2. runtime_alias: if missing, use id
            if not acc.get("runtime_alias"):
                raw_id = str(acc.get("id") or "").strip()
                acc["runtime_alias"] = raw_id or f"acc_{acc['instance_uuid'][:8]}"

            # 3. id: backward-compatibility alias, must equal runtime_alias
            acc["id"] = acc["runtime_alias"]

            # 4. display_name: if missing, use display_name or runtime_alias
            if not acc.get("display_name"):
                acc["display_name"] = acc["runtime_alias"]

            # 5. resource_key: solidify existing resources so NO renaming occurs
            if not acc.get("resource_key"):
                acc["resource_key"] = derive_legacy_resource_key(acc)

            # 6. runtime_provider
            if not acc.get("runtime_provider"):
                acc["runtime_provider"] = "agent_wechat" if "agent_wechat" in acc else "legacy"

            # 7. agent_wechat inner consistency
            if acc["runtime_provider"] == "agent_wechat" and "agent_wechat" in acc:
                aw = acc["agent_wechat"]
                if isinstance(aw, dict):
                    safe = acc["resource_key"]
                    aw.setdefault("container_name", f"wechat-agent-{safe}")
                    aw.setdefault("data_volume", f"wechat-agent-{safe}-data")
                    aw.setdefault("home_volume", f"wechat-agent-{safe}-home")
                    aw.setdefault("token_file", f"/config/agent-wechat/{safe}/auth-token")

        return new_data

    def save(self, data: Dict[str, Any]) -> None:
        self._validate(data)
        self.paths.registry_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.paths.registry_file.with_suffix(
            self.paths.registry_file.suffix + ".tmp"
        )
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, self.paths.registry_file)

    def _initial_registry(self) -> Dict[str, Any]:
        raw_accounts = os.environ.get("WECHAT_ACCOUNTS")
        account_ids = parse_account_ids(raw_accounts)
        display_map = parse_display_map(os.environ.get("WECHAT_ACCOUNT_DISPLAY_MAP"))
        default_display = os.environ.get("DISPLAY", DEFAULT_DISPLAY)
        uid_base = int(os.environ.get("WECHAT_ACCOUNT_UID_BASE", DEFAULT_UID_BASE))
        legacy_default = bool_env("WECHAT_LEGACY_DEFAULT_ACCOUNT", True)

        accounts: List[Dict[str, Any]] = []
        next_uid = uid_base
        for account_id in account_ids:
            use_legacy_abc = account_id == DEFAULT_ACCOUNT_ID and legacy_default
            if use_legacy_abc:
                username = "abc"
                uid = None
                home = "/config"
            else:
                username = account_username(account_id)
                uid = next_uid
                next_uid += 1
                home = str(self.paths.account_home_root / account_id / "home")
            accounts.append(
                {
                    "instance_uuid": str(uuid.uuid4()),
                    "runtime_alias": account_id,
                    "id": account_id,
                    "display_name": account_id,
                    "resource_key": sanitize_account_runtime_name(account_id),
                    "username": username,
                    "uid": uid,
                    "display": display_map.get(account_id, default_display),
                    "home": home,
                    "enabled": True,
                    "autostart": True,
                    "legacy": use_legacy_abc,
                    "runtime_provider": "legacy",
                }
            )

        return {
            "version": REGISTRY_VERSION,
            "created_at": int(time.time()),
            "accounts": accounts,
        }

    @staticmethod
    def _validate(data: Dict[str, Any]) -> None:
        version = data.get("version")
        if version not in (1, 2):
            raise RuntimeErrorWithHint(
                f"unsupported registry version: {version!r}"
            )
        accounts = data.get("accounts")
        if not isinstance(accounts, list):
            raise RuntimeErrorWithHint("registry accounts must be a list")
        seen_aliases = set()
        seen_uuids = set()
        seen_users = set()
        seen_resource_keys = set()
        for account in accounts:
            if not isinstance(account, dict):
                raise RuntimeErrorWithHint("registry account entry must be an object")
            raw_alias = account.get("runtime_alias") or account.get("id")
            if not raw_alias:
                raise RuntimeErrorWithHint("missing account id or runtime_alias")
            account_id = validate_runtime_alias(str(raw_alias))
            runtime_provider(account)

            if version == 2 or "instance_uuid" in account:
                raw_uuid = account.get("instance_uuid")
                if not raw_uuid:
                    raise RuntimeErrorWithHint(f"missing instance_uuid for account {account_id}")
                validated_uuid = validate_instance_uuid(str(raw_uuid)).lower()
                if validated_uuid in seen_uuids:
                    raise RuntimeErrorWithHint(f"duplicate instance_uuid: {raw_uuid}")
                seen_uuids.add(validated_uuid)

            if version == 2 or "resource_key" in account:
                res_key = str(account.get("resource_key") or "").strip()
                if not res_key:
                    raise RuntimeErrorWithHint(f"missing resource_key for account {account_id}")
                if res_key in seen_resource_keys:
                    raise RuntimeErrorWithHint(f"duplicate resource_key: {res_key}")
                seen_resource_keys.add(res_key)

            username = str(account.get("username", ""))
            if not username:
                raise RuntimeErrorWithHint(f"missing username for {account_id}")
            if account_id in seen_aliases:
                raise RuntimeErrorWithHint(f"duplicate account id: {account_id}")
            if username in seen_users:
                raise RuntimeErrorWithHint(f"duplicate Unix username: {username}")
            seen_aliases.add(account_id)
            seen_users.add(username)


def find_account(data: Dict[str, Any], identifier: str) -> Dict[str, Any]:
    target = str(identifier).strip()
    if not target:
        raise RuntimeErrorWithHint("account identifier cannot be empty")
    target_lower = target.lower()

    # 1. Search by exact instance_uuid
    for account in data.get("accounts", []):
        u = str(account.get("instance_uuid") or "").strip().lower()
        if u and u == target_lower:
            return account
    # 2. Search by runtime_alias or id
    for account in data.get("accounts", []):
        if str(account.get("runtime_alias") or "") == target or str(account.get("id") or "") == target:
            return account
    # 3. Search by resource_key
    for account in data.get("accounts", []):
        if str(account.get("resource_key") or "") == target:
            return account
    raise RuntimeErrorWithHint(f"unknown WeChat account: {target}")
def next_registry_uid(data: Dict[str, Any]) -> int:
    configured = [
        int(account["uid"])
        for account in data["accounts"]
        if account.get("uid") is not None and not account.get("legacy")
    ]
    base = int(os.environ.get("WECHAT_ACCOUNT_UID_BASE", DEFAULT_UID_BASE))
    return max(configured + [base - 1]) + 1


def require_root(action: str) -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeErrorWithHint(
            f"{action} requires root inside the container; use /scripts/wechat/wechat-runtime"
        )


def _lookup_user(username: str):
    if pwd is None:
        raise RuntimeErrorWithHint("Unix passwd database is unavailable on this platform")
    try:
        return pwd.getpwnam(username)
    except KeyError:
        return None


def _uid_in_use(uid: int) -> bool:
    if pwd is None:
        raise RuntimeErrorWithHint("Unix passwd database is unavailable on this platform")
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def _abc_group_name() -> str:
    if grp is None:
        raise RuntimeErrorWithHint("Unix group database is unavailable on this platform")
    abc = _lookup_user("abc")
    if abc is None:
        raise RuntimeErrorWithHint(
            "Selkies desktop user 'abc' is missing; account bootstrap ran too early"
        )
    return grp.getgrgid(abc.pw_gid).gr_name


def _abc_device_groups() -> List[str]:
    if grp is None or pwd is None:
        raise RuntimeErrorWithHint("Unix group database is unavailable on this platform")
    abc = _lookup_user("abc")
    if abc is None:
        raise RuntimeErrorWithHint("Selkies desktop user 'abc' is missing")
    result: List[str] = []
    for group in grp.getgrall():
        if group.gr_name not in DEVICE_GROUP_ALLOWLIST:
            continue
        if group.gr_gid == abc.pw_gid or "abc" in group.gr_mem:
            result.append(group.gr_name)
    return sorted(set(result))


def ensure_directory(path: Path, uid: int, gid: int, mode: int = 0o770) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(path, uid, gid)
    except PermissionError as exc:
        raise RuntimeErrorWithHint(f"cannot chown {path}: {exc}") from exc
    os.chmod(path, mode)


def bootstrap_account(account: Dict[str, Any], paths: RuntimePaths) -> bool:
    """Create/reconcile one Unix account. Returns True if registry changed."""

    require_root("bootstrap")
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        # agent-wechat owns its own Unix user/Xvfb/AT-SPI environment inside
        # the child container. Runtime only prepares persistent account data.
        AgentWechatManager().prepare_files(account)
        return False
    changed = False
    username = account["username"]

    if account.get("legacy"):
        entry = _lookup_user(username)
        if entry is None:
            raise RuntimeErrorWithHint(
                f"legacy account expects existing Selkies user {username!r}"
            )
        if account.get("uid") != entry.pw_uid:
            account["uid"] = entry.pw_uid
            changed = True
        # LinuxServer owns /config and the abc home lifecycle; do not recursively chown it.
        return changed

    desired_uid = int(account["uid"])
    entry = _lookup_user(username)
    if entry is None:
        while _uid_in_use(desired_uid):
            desired_uid += 1
        group_name = _abc_group_name()
        home = str(Path(account["home"]))
        subprocess.run(
            [
                "useradd",
                "--uid",
                str(desired_uid),
                "--gid",
                group_name,
                "--home-dir",
                home,
                "--no-create-home",
                "--shell",
                "/bin/bash",
                username,
            ],
            check=True,
        )
        entry = pwd.getpwnam(username)
        if account.get("uid") != entry.pw_uid:
            account["uid"] = entry.pw_uid
            changed = True
    elif account.get("uid") != entry.pw_uid:
        account["uid"] = entry.pw_uid
        changed = True

    # Reconcile the dedicated user with the current Selkies abc primary group.
    # This also makes bootstrap robust if a base-image init ordering change ever
    # creates the account before PUID/PGID reconciliation completes.
    abc = _lookup_user("abc")
    if abc is None:
        raise RuntimeErrorWithHint("Selkies desktop user 'abc' is missing")
    if entry.pw_gid != abc.pw_gid:
        subprocess.run(
            ["usermod", "--gid", _abc_group_name(), username],
            check=True,
        )
        entry = pwd.getpwnam(username)

    expected_home = str(Path(account["home"]))
    if entry.pw_dir != expected_home:
        subprocess.run(
            ["usermod", "--home", expected_home, username],
            check=True,
        )
        entry = pwd.getpwnam(username)

    # Preserve GPU/audio/input access needed by the Selkies desktop without
    # cloning privileged groups such as sudo or docker into account users.
    device_groups = _abc_device_groups()
    if device_groups:
        subprocess.run(
            ["usermod", "--append", "--groups", ",".join(device_groups), username],
            check=True,
        )

    home = Path(account["home"])
    ensure_directory(home, entry.pw_uid, entry.pw_gid)
    ensure_directory(home / ".config", entry.pw_uid, entry.pw_gid)
    ensure_directory(home / ".local", entry.pw_uid, entry.pw_gid)
    ensure_directory(home / ".local" / "share", entry.pw_uid, entry.pw_gid)
    ensure_directory(home / ".cache", entry.pw_uid, entry.pw_gid)
    ensure_directory(paths.runtime_dir / "accounts" / account["id"], entry.pw_uid, entry.pw_gid)

    xdg_runtime = Path("/run/user") / str(entry.pw_uid)
    ensure_directory(xdg_runtime, entry.pw_uid, entry.pw_gid, mode=0o700)
    return changed


def bootstrap_all(registry: Registry) -> Dict[str, Any]:
    require_root("bootstrap")
    with registry.locked():
        # The runtime directory may be shared with Core and may survive a
        # container restart.  A stale readiness marker must never outlive the
        # bootstrap that produced it.
        ready = registry.paths.runtime_dir / "bootstrap.ready"
        ready.unlink(missing_ok=True)
        data = registry.load(create=True)
        registry.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        locks = registry.paths.runtime_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        os.chmod(locks, 0o1777)

        changed = False
        for account in data["accounts"]:
            changed = bootstrap_account(account, registry.paths) or changed
        if changed:
            registry.save(data)

        ready.write_text(str(int(time.time())) + "\n", encoding="utf-8")
        os.chmod(ready, 0o644)
        return data


def account_environment(account: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    home = str(account["home"])
    env["WECHAT_ACCOUNT_ID"] = account["id"]
    env["USER"] = account["username"]
    env["LOGNAME"] = account["username"]
    env["HOME"] = home
    env["DISPLAY"] = account.get("display") or env.get("DISPLAY", DEFAULT_DISPLAY)

    if account.get("legacy"):
        env.setdefault("XDG_CONFIG_HOME", posixpath.join(home, ".config"))
        env.setdefault("XDG_DATA_HOME", posixpath.join(home, ".local", "share"))
        env.setdefault("XDG_CACHE_HOME", posixpath.join(home, ".cache"))
    else:
        env["XDG_CONFIG_HOME"] = posixpath.join(home, ".config")
        env["XDG_DATA_HOME"] = posixpath.join(home, ".local", "share")
        env["XDG_CACHE_HOME"] = posixpath.join(home, ".cache")
        # The abc desktop session bus normally authenticates as abc and must
        # not be reused by another Unix UID. start_account creates an isolated
        # dbus-run-session for dedicated accounts when the tool is available.
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        env.pop("SESSION_MANAGER", None)
    if account.get("uid") is not None:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{int(account['uid'])}"
    return env


def user_exec_prefix(account: Dict[str, Any]) -> List[str]:
    require_root("account process launch")
    username = account["username"]
    if shutil.which("runuser"):
        return ["runuser", "--user", username, "--preserve-environment", "--"]
    if shutil.which("setpriv") and account.get("uid") is not None:
        if pwd is None:
            raise RuntimeErrorWithHint("Unix passwd database is unavailable")
        entry = pwd.getpwnam(username)
        return [
            "setpriv",
            "--reuid",
            str(entry.pw_uid),
            "--regid",
            str(entry.pw_gid),
            "--init-groups",
        ]
    raise RuntimeErrorWithHint("neither runuser nor setpriv is available")


def _proc_uid(pid: int) -> Optional[int]:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except (FileNotFoundError, PermissionError):
        return None


def _proc_text(pid: int, name: str) -> str:
    try:
        data = Path(f"/proc/{pid}/{name}").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return ""
    if name == "cmdline":
        return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    return data.decode("utf-8", errors="replace").strip()


def is_wechat_process(pid: int) -> bool:
    cmdline = _proc_text(pid, "cmdline")
    comm = _proc_text(pid, "comm").lower()
    lower = cmdline.lower()
    if "/scripts/wechat/" in lower or "wechat_runtime.py" in lower:
        return False
    if "/usr/bin/wechat" in lower or "/opt/wechat/" in lower or "/usr/lib/wechat" in lower:
        return True
    return comm.startswith("wechat") or comm.startswith("weixin")


def account_processes(account: Dict[str, Any]) -> List[int]:
    if account.get("uid") is None or not Path("/proc").exists():
        return []
    uid = int(account["uid"])
    result: List[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or _proc_uid(pid) != uid:
            continue
        if is_wechat_process(pid):
            result.append(pid)
    return sorted(result)


def _window_title(win: Any, display_obj: Any) -> str:
    for atom_name in ("_NET_WM_NAME", "WM_NAME"):
        try:
            prop = win.get_full_property(display_obj.intern_atom(atom_name), 0)
            if prop is not None and prop.value is not None:
                value = prop.value
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return str(value)
        except Exception:
            continue
    return ""


def account_windows(account: Dict[str, Any]) -> Dict[str, Any]:
    """Map X11 client windows to the account UID using _NET_WM_PID."""

    display_name = account.get("display") or DEFAULT_DISPLAY
    uid = account.get("uid")
    if uid is None:
        return {"display": display_name, "windows": [], "error": "uid unresolved"}
    try:
        from Xlib import X, display as xdisplay  # type: ignore
    except Exception as exc:
        return {
            "display": display_name,
            "windows": [],
            "error": f"python-xlib unavailable: {exc}",
        }

    try:
        d = xdisplay.Display(display_name)
        root = d.screen().root
        clients = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        if not clients:
            d.close()
            return {"display": display_name, "windows": [], "error": None}
        pid_atom = d.intern_atom("_NET_WM_PID")
        windows: List[Dict[str, Any]] = []
        for wid in clients.value:
            try:
                win = d.create_resource_object("window", int(wid))
                pid_prop = win.get_full_property(pid_atom, X.AnyPropertyType)
                if not pid_prop or len(pid_prop.value) == 0:
                    continue
                pid = int(pid_prop.value[0])
                if _proc_uid(pid) != int(uid):
                    continue
                if not is_wechat_process(pid):
                    # Some clients put a child helper PID on the window. For a
                    # dedicated account Unix user, UID ownership is still an
                    # account-safe correlation; for legacy abc stay strict.
                    if account.get("legacy"):
                        continue
                geometry = win.get_geometry()
                windows.append(
                    {
                        "window_id": int(wid),
                        "pid": pid,
                        "title": _window_title(win, d),
                        "width": int(getattr(geometry, "width", 0) or 0),
                        "height": int(getattr(geometry, "height", 0) or 0),
                    }
                )
            except Exception:
                continue
        d.close()
        return {"display": display_name, "windows": windows, "error": None}
    except Exception as exc:
        return {"display": display_name, "windows": [], "error": str(exc)}


def status_for(account: Dict[str, Any]) -> Dict[str, Any]:
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        return AgentWechatManager().status(account)
    pids = account_processes(account)
    windows = account_windows(account)
    alias = str(account.get("runtime_alias") or account.get("id") or "")
    display = str(account.get("display_name") or alias)
    uuid_val = str(account.get("instance_uuid") or "")
    res_key = str(account.get("resource_key") or "")
    return {
        "instance_uuid": uuid_val,
        "runtime_alias": alias,
        "account_id": account["id"],
        "display_name": display,
        "resource_key": res_key,
        "container_id": "",
        "runtime_provider": "legacy",
        "logged_in_user": "",
        "identity_observed_at": None,
        "wechat_profile": None,
        "enabled": bool(account.get("enabled", True)),
        "autostart": bool(account.get("autostart", True)),
        "legacy": bool(account.get("legacy", False)),
        "username": account["username"],
        "uid": account.get("uid"),
        "home": account["home"],
        "display": account.get("display") or DEFAULT_DISPLAY,
        "running": bool(pids),
        "pids": pids,
        "windows": windows["windows"],
        "window_error": windows["error"],
        "display_lock": str(
            RuntimePaths.from_env().runtime_dir
            / "locks"
            / f"display-{display_lock_name(account.get('display') or DEFAULT_DISPLAY)}.lock"
        ),
    }


def _open_account_log(account: Dict[str, Any], paths: RuntimePaths):
    log_dir = paths.registry_file.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return (log_dir / f"{account['id']}.log").open("ab", buffering=0)


def _spawn_as_account(
    account: Dict[str, Any], argv: Sequence[str], paths: RuntimePaths
) -> subprocess.Popen:
    env = account_environment(account)
    log_handle = _open_account_log(account, paths)
    command = user_exec_prefix(account) + list(argv)
    try:
        proc = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    return proc


def _spawn_auto_login(account: Dict[str, Any], paths: RuntimePaths) -> None:
    if not bool_env("ENABLE_WECHAT_AUTO_LOGIN", True):
        return
    python_bin = "/lsiopy/bin/python3"
    if not Path(python_bin).exists():
        python_bin = sys.executable
    _spawn_as_account(
        account,
        [python_bin, "/scripts/wechat/wechat-auto-login.py", "--account", account["id"]],
        paths,
    )


def _show_account_window(account: Dict[str, Any]) -> bool:
    """Restore an existing WeChat surface without touching other accounts."""

    windows = account_windows(account)["windows"]
    if not windows:
        return False
    selected = next(
        (item for item in windows if item.get("title") == "Weixin"),
        windows[0],
    )
    try:
        subprocess.run(
            [
                "/scripts/wechat/wechat-display-lock.sh",
                account["id"],
                "xdotool",
                "windowactivate",
                "--sync",
                str(selected["window_id"]),
            ],
            check=True,
            env=account_environment(account),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def start_account(account: Dict[str, Any], paths: RuntimePaths) -> Dict[str, Any]:
    require_root("start")
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        AgentWechatManager().prepare_files(account)
        return AgentWechatManager().start(account)
    bootstrap_account(account, paths)
    existing = account_processes(account)
    if existing:
        result = status_for(account)
        if _show_account_window(account):
            result["action"] = "restored"
        else:
            result["action"] = "already-running"
        return result

    wechat_bin = os.environ.get("WECHAT_BINARY", "/usr/bin/wechat")
    if not Path(wechat_bin).exists():
        raise RuntimeErrorWithHint(f"WeChat binary not found: {wechat_bin}")

    launch_argv = [wechat_bin]
    if not account.get("legacy") and shutil.which("dbus-run-session"):
        launch_argv = ["dbus-run-session", "--", wechat_bin]
    _spawn_as_account(account, launch_argv, paths)
    timeout = float(os.environ.get("WECHAT_START_DISCOVERY_TIMEOUT", "5"))
    deadline = time.monotonic() + max(timeout, 0.0)
    while time.monotonic() < deadline:
        if account_processes(account):
            break
        time.sleep(0.2)
    _spawn_auto_login(account, paths)
    result = status_for(account)
    result["action"] = "started" if result["running"] else "launch-dispatched"
    return result


def _signal_processes(pids: Iterable[int], sig: int) -> None:
    for pid in sorted(set(pids), reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise RuntimeErrorWithHint(f"cannot signal pid {pid}: {exc}") from exc


def stop_account(account: Dict[str, Any], paths: RuntimePaths) -> Dict[str, Any]:
    del paths
    require_root("stop")
    if runtime_provider(account) == "agent_wechat":
        from agent_wechat_runtime import AgentWechatManager

        return AgentWechatManager().stop(account)
    pids = account_processes(account)
    if not pids:
        result = status_for(account)
        result["action"] = "already-stopped"
        return result

    _signal_processes(pids, signal.SIGTERM)
    timeout = float(os.environ.get("WECHAT_STOP_TIMEOUT", "5"))
    deadline = time.monotonic() + max(timeout, 0.0)
    while time.monotonic() < deadline:
        remaining = account_processes(account)
        if not remaining:
            break
        time.sleep(0.2)
    remaining = account_processes(account)
    if remaining:
        _signal_processes(remaining, signal.SIGKILL)
    result = status_for(account)
    result["action"] = "stopped"
    return result


def restart_account(account: Dict[str, Any], paths: RuntimePaths) -> Dict[str, Any]:
    stop_account(account, paths)
    return start_account(account, paths)


def print_result(value: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print(item.get("account_id") or item.get("id") or json.dumps(item))
            else:
                print(item)
    else:
        print(value)


def register_account(
    registry: Registry,
    account_id: str,
    display_name: Optional[str],
    autostart: bool,
    label: Optional[str] = None,
    provider: str = "legacy",
    instance_uuid: Optional[str] = None,
    runtime_alias: Optional[str] = None,
    resource_key: Optional[str] = None,
) -> Dict[str, Any]:
    alias = validate_runtime_alias(str(runtime_alias or account_id).strip())
    require_root("register")
    provider = str(provider or "legacy").strip().lower()
    if provider not in RUNTIME_PROVIDERS:
        raise ValueError(f"runtime_provider must be one of {sorted(RUNTIME_PROVIDERS)}")

    with registry.locked():
        data = registry.load(create=True)
        if any(item.get("runtime_alias") == alias or item.get("id") == alias for item in data["accounts"]):
            raise RuntimeErrorWithHint(f"account already exists: {alias}")

        if instance_uuid:
            uuid_val = validate_instance_uuid(instance_uuid)
            if any(str(item.get("instance_uuid") or "").lower() == uuid_val.lower() for item in data["accounts"]):
                raise RuntimeErrorWithHint(f"instance_uuid already exists: {uuid_val}")
        else:
            uuid_val = str(uuid.uuid4())

        res_key = str(resource_key or "").strip() or sanitize_account_runtime_name(alias)
        if any(item.get("resource_key") == res_key for item in data["accounts"]):
            raise RuntimeErrorWithHint(f"resource_key already exists: {res_key}")

        name = (label or display_name or alias).strip() or alias

        if provider == "agent_wechat":
            from agent_wechat_runtime import AgentWechatManager

            safe_name = res_key
            account = {
                "instance_uuid": uuid_val,
                "runtime_alias": alias,
                "display_name": name,
                "resource_key": res_key,
                "id": alias,
                "username": f"agent_{safe_name}",
                "uid": None,
                "display": "isolated",
                "home": f"/config/agent-wechat/{safe_name}/home",
                "enabled": True,
                "autostart": autostart,
                "legacy": False,
                "runtime_provider": "agent_wechat",
            }
            manager = AgentWechatManager()
            data_volume, home_volume = manager.storage_names(account)
            account["agent_wechat"] = {
                "container_name": manager.container_name(account),
                "token_file": f"/config/agent-wechat/{safe_name}/auth-token",
                "data_volume": data_volume,
                "home_volume": home_volume,
            }
        else:
            uid = next_registry_uid(data)
            account = {
                "instance_uuid": uuid_val,
                "runtime_alias": alias,
                "display_name": name,
                "resource_key": res_key,
                "id": alias,
                "username": account_username(alias),
                "uid": uid,
                "display": display_name or os.environ.get("DISPLAY", DEFAULT_DISPLAY),
                "home": str(registry.paths.account_home_root / alias / "home"),
                "enabled": True,
                "autostart": autostart,
                "legacy": False,
                "runtime_provider": "legacy",
            }
        data["accounts"].append(account)
        bootstrap_account(account, registry.paths)
        registry.save(data)
        return account


def update_account(
    registry: Registry,
    identifier: str,
    *,
    display_name: Optional[str] = None,
    runtime_alias: Optional[str] = None,
    enabled: Optional[bool] = None,
    autostart: Optional[bool] = None,
) -> Dict[str, Any]:
    require_root("update")
    with registry.locked():
        data = registry.load(create=True)
        account = find_account(data, identifier)
        changed = False

        if display_name is not None:
            cleaned_display = str(display_name).strip()
            if not cleaned_display:
                raise ValueError("display_name cannot be empty")
            account["display_name"] = cleaned_display
            changed = True

        if runtime_alias is not None:
            new_alias = validate_runtime_alias(str(runtime_alias).strip())
            old_alias = account.get("runtime_alias") or account.get("id")
            if new_alias != old_alias:
                for other in data["accounts"]:
                    if other is account:
                        continue
                    if other.get("runtime_alias") == new_alias or other.get("id") == new_alias:
                        raise RuntimeErrorWithHint(f"runtime_alias already in use: {new_alias}")
                account["runtime_alias"] = new_alias
                account["id"] = new_alias
                changed = True

        if enabled is not None:
            account["enabled"] = bool(enabled)
            changed = True

        if autostart is not None:
            account["autostart"] = bool(autostart)
            changed = True

        if changed:
            registry.save(data)
        return account


def unregister_account(
    registry: Registry, identifier: str, *, purge_data: bool = False
) -> Dict[str, Any]:
    require_root("unregister")
    with registry.locked():
        data = registry.load(create=True)
        account = find_account(data, identifier)
        target_uuid = account.get("instance_uuid")
        target_id = account.get("id")
        target_alias = account.get("runtime_alias")
        if runtime_provider(account) == "agent_wechat":
            from agent_wechat_runtime import AgentWechatManager

            removal = AgentWechatManager().remove(account, purge_data=purge_data)
        else:
            stop_account(account, registry.paths)
            removal = {
                "instance_uuid": str(target_uuid or ""),
                "runtime_alias": str(target_alias or target_id or ""),
                "removed": target_id,
                "data_preserved": account["home"],
                "unix_user_preserved": account["username"],
                "preserve_data": True,
                "runtime_provider": "legacy",
            }
        data["accounts"] = [
            item for item in data["accounts"]
            if not (
                (target_uuid and item.get("instance_uuid") == target_uuid)
                or (not target_uuid and item.get("id") == target_id)
            )
        ]
        registry.save(data)
        return removal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-account WeChat runtime manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="create registry, users and account directories")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list registered accounts")
    p.add_argument("--json", action="store_true")

    for name in ("start", "stop", "restart", "status", "display", "window"):
        p = sub.add_parser(name)
        p.add_argument("account", nargs="?", default=os.environ.get("WECHAT_DEFAULT_ACCOUNT_ID", DEFAULT_ACCOUNT_ID))
        if name in {"start", "stop", "restart", "status"}:
            p.add_argument("--json", action="store_true")

    for name in ("start-all", "stop-all", "restart-all"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--autostart-only", action="store_true")

    p = sub.add_parser("health", help="overall runtime health")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("register", help="add a persistent account")
    p.add_argument("account")
    p.add_argument("--name", help="human-friendly account display name")
    p.add_argument("--display")
    p.add_argument("--provider", choices=sorted(RUNTIME_PROVIDERS), default="legacy")
    p.add_argument("--uuid", dest="instance_uuid", help="specific instance UUID")
    p.add_argument("--alias", dest="runtime_alias", help="specific runtime alias")
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("update", help="update display name or runtime alias")
    p.add_argument("account")
    p.add_argument("--name", help="new display name")
    p.add_argument("--alias", help="new runtime alias")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unregister", help="remove registry entry, preserving home and Unix user")
    p.add_argument("account")
    p.add_argument("--purge-data", action="store_true", help="also delete agent-wechat persistent data")
    p.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry(RuntimePaths.from_env())

    try:
        if args.command == "bootstrap":
            data = bootstrap_all(registry)
            print_result(data, args.json)
            return 0

        data = registry.load(create=args.command in {"register"})

        if args.command == "list":
            print_result(data["accounts"], args.json)
            return 0

        if args.command == "register":
            result = register_account(
                registry,
                args.account,
                args.display,
                not args.no_autostart,
                args.name,
                args.provider,
                instance_uuid=args.instance_uuid,
                runtime_alias=args.runtime_alias,
            )
            print_result(result, args.json)
            return 0

        if args.command == "update":
            alias_req = str(args.alias).strip() if args.alias is not None else None
            account = find_account(data, args.account)
            current_alias = str(account.get("runtime_alias") or account.get("id") or "").strip()
            if alias_req and alias_req != current_alias:
                raise RuntimeErrorWithHint(
                    "runtime_alias rename is deferred in this release to protect legacy account bindings; display_name can be updated freely"
                )
            result = update_account(
                registry,
                args.account,
                display_name=args.name,
                runtime_alias=alias_req,
            )
            print_result(result, args.json)
            return 0

        if args.command == "unregister":
            result = unregister_account(registry, args.account, purge_data=args.purge_data)
            print_result(result, args.json)
            return 0

        if args.command in {"start", "stop", "restart", "status", "display", "window"}:
            account = find_account(data, args.account)
            if args.command == "display":
                print(account.get("display") or DEFAULT_DISPLAY)
                return 0
            if args.command == "window":
                windows = account_windows(account)["windows"]
                selected = next(
                    (item for item in windows if item.get("title") == "Weixin"),
                    windows[0] if windows else None,
                )
                if selected is None:
                    raise RuntimeErrorWithHint(
                        f"no WeChat window discovered for account {args.account}"
                    )
                print(selected["window_id"])
                return 0
            if args.command == "start":
                result = start_account(account, registry.paths)
            elif args.command == "stop":
                result = stop_account(account, registry.paths)
            elif args.command == "restart":
                result = restart_account(account, registry.paths)
            else:
                result = status_for(account)
            print_result(result, args.json)
            return 0

        if args.command in {"start-all", "stop-all", "restart-all"}:
            action = {
                "start-all": start_account,
                "stop-all": stop_account,
                "restart-all": restart_account,
            }[args.command]
            results = []
            for account in data["accounts"]:
                if not account.get("enabled", True):
                    continue
                if args.autostart_only and not account.get("autostart", True):
                    continue
                results.append(action(account, registry.paths))
            print_result(results, args.json)
            return 0

        if args.command == "health":
            statuses = [status_for(account) for account in data["accounts"]]
            expected = [item for item in statuses if item["enabled"] and item["autostart"]]
            healthy = bool(expected) and all(item["running"] for item in expected)
            result = {
                "healthy": healthy,
                "accounts": statuses,
                "registry": str(registry.paths.registry_file),
            }
            print_result(result, args.json)
            return 0 if healthy else 1

        raise AssertionError(args.command)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"wechat-runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
