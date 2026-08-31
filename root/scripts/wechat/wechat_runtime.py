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
import time
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


REGISTRY_VERSION = 1
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_UID_BASE = 20000
DEFAULT_DISPLAY = ":1"
DEVICE_GROUP_ALLOWLIST = {"audio", "input", "plugdev", "render", "video"}


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

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.paths.runtime_dir / "registry.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self, create: bool = False) -> Dict[str, Any]:
        if self.paths.registry_file.exists():
            data = json.loads(self.paths.registry_file.read_text(encoding="utf-8"))
            self._validate(data)
            return data
        if not create:
            raise RuntimeErrorWithHint(
                f"registry does not exist: {self.paths.registry_file}; run bootstrap"
            )
        data = self._initial_registry()
        self.save(data)
        return data

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
                    "id": account_id,
                    "username": username,
                    "uid": uid,
                    "display": display_map.get(account_id, default_display),
                    "home": home,
                    "enabled": True,
                    "autostart": True,
                    "legacy": use_legacy_abc,
                }
            )

        return {
            "version": REGISTRY_VERSION,
            "created_at": int(time.time()),
            "accounts": accounts,
        }

    @staticmethod
    def _validate(data: Dict[str, Any]) -> None:
        if data.get("version") != REGISTRY_VERSION:
            raise RuntimeErrorWithHint(
                f"unsupported registry version: {data.get('version')!r}"
            )
        accounts = data.get("accounts")
        if not isinstance(accounts, list):
            raise RuntimeErrorWithHint("registry accounts must be a list")
        seen_ids = set()
        seen_users = set()
        for account in accounts:
            if not isinstance(account, dict):
                raise RuntimeErrorWithHint("registry account entry must be an object")
            account_id = validate_account_id(str(account.get("id", "")))
            username = str(account.get("username", ""))
            if not username:
                raise RuntimeErrorWithHint(f"missing username for {account_id}")
            if account_id in seen_ids:
                raise RuntimeErrorWithHint(f"duplicate account id: {account_id}")
            if username in seen_users:
                raise RuntimeErrorWithHint(f"duplicate Unix username: {username}")
            seen_ids.add(account_id)
            seen_users.add(username)


def find_account(data: Dict[str, Any], account_id: str) -> Dict[str, Any]:
    validate_account_id(account_id)
    for account in data["accounts"]:
        if account["id"] == account_id:
            return account
    raise RuntimeErrorWithHint(f"unknown WeChat account: {account_id}")


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

        ready = registry.paths.runtime_dir / "bootstrap.ready"
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
                windows.append(
                    {
                        "window_id": int(wid),
                        "pid": pid,
                        "title": _window_title(win, d),
                    }
                )
            except Exception:
                continue
        d.close()
        return {"display": display_name, "windows": windows, "error": None}
    except Exception as exc:
        return {"display": display_name, "windows": [], "error": str(exc)}


def status_for(account: Dict[str, Any]) -> Dict[str, Any]:
    pids = account_processes(account)
    windows = account_windows(account)
    return {
        "account_id": account["id"],
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


def start_account(account: Dict[str, Any], paths: RuntimePaths) -> Dict[str, Any]:
    require_root("start")
    bootstrap_account(account, paths)
    existing = account_processes(account)
    if existing:
        result = status_for(account)
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
    registry: Registry, account_id: str, display_name: Optional[str], autostart: bool
) -> Dict[str, Any]:
    validate_account_id(account_id)
    require_root("register")
    with registry.locked():
        data = registry.load(create=True)
        if any(item["id"] == account_id for item in data["accounts"]):
            raise RuntimeErrorWithHint(f"account already exists: {account_id}")
        uid = next_registry_uid(data)
        account = {
            "id": account_id,
            "username": account_username(account_id),
            "uid": uid,
            "display": display_name or os.environ.get("DISPLAY", DEFAULT_DISPLAY),
            "home": str(registry.paths.account_home_root / account_id / "home"),
            "enabled": True,
            "autostart": autostart,
            "legacy": False,
        }
        data["accounts"].append(account)
        bootstrap_account(account, registry.paths)
        registry.save(data)
        return account


def unregister_account(registry: Registry, account_id: str) -> Dict[str, Any]:
    require_root("unregister")
    with registry.locked():
        data = registry.load(create=True)
        account = find_account(data, account_id)
        stop_account(account, registry.paths)
        data["accounts"] = [item for item in data["accounts"] if item["id"] != account_id]
        registry.save(data)
        return {
            "removed": account_id,
            "data_preserved": account["home"],
            "unix_user_preserved": account["username"],
        }


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
    p.add_argument("--display")
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unregister", help="remove registry entry, preserving home and Unix user")
    p.add_argument("account")
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
                registry, args.account, args.display, not args.no_autostart
            )
            print_result(result, args.json)
            return 0

        if args.command == "unregister":
            result = unregister_account(registry, args.account)
            print_result(result, args.json)
            return 0

        if args.command in {"start", "stop", "restart", "status", "display", "window"}:
            account = find_account(data, args.account)
            if args.command == "display":
                print(account.get("display") or DEFAULT_DISPLAY)
                return 0
            if args.command == "window":
                windows = account_windows(account)["windows"]
                if not windows:
                    raise RuntimeErrorWithHint(
                        f"no WeChat window discovered for account {args.account}"
                    )
                print(windows[0]["window_id"])
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
    except (RuntimeErrorWithHint, ValueError, subprocess.CalledProcessError) as exc:
        print(f"wechat-runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

