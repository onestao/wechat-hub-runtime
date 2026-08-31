#!/usr/bin/env python3
"""Best-effort, account-safe WeChat auto-login helper.

The upstream helper searched the whole DISPLAY and returned the first large
visible window. That is unsafe once multiple official WeChat processes share a
display. This version only considers WeChat-looking windows whose _NET_WM_PID
belongs to the current Unix account and serializes all UI automation through a
per-DISPLAY flock.
"""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux runtime dependency
    fcntl = None

from PIL import ImageGrab


ENABLE_AUTO_LOGIN = os.environ.get("ENABLE_WECHAT_AUTO_LOGIN", "true").lower() == "true"
AUTO_LOGIN_DELAY = int(os.environ.get("AUTO_LOGIN_DELAY", "3"))
DISPLAY_NAME = os.environ.get("DISPLAY", ":1")
RUNTIME_DIR = Path(os.environ.get("WECHAT_RUNTIME_DIR", "/run/wechat-runtime"))
WECHAT_MARKERS = ("wechat", "weixin", "微信")


def display_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY_NAME
    return env


def run_xdotool(*args: str) -> str:
    return subprocess.check_output(
        ["xdotool", *args],
        env=display_env(),
        stderr=subprocess.DEVNULL,
    ).decode("utf-8", errors="replace").strip()


def proc_uid(pid: int) -> Optional[int]:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except (FileNotFoundError, PermissionError, OSError):
        return None


def get_geometry(wid: str) -> Dict[str, int]:
    output = run_xdotool("getwindowgeometry", "--shell", wid)
    result: Dict[str, int] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            result[key] = int(value)
        except ValueError:
            continue
    return result


def looks_like_wechat(title: str, class_name: str) -> bool:
    value = f"{title} {class_name}".lower()
    return any(marker in value for marker in WECHAT_MARKERS)


def candidate_windows() -> List[Tuple[str, str, str, Dict[str, int]]]:
    """Return visible WeChat windows owned by the current Unix UID."""

    current_uid = os.getuid() if hasattr(os, "getuid") else None
    try:
        # A single dot is a regex that matches non-empty titles. We still
        # verify class/title and process UID below before taking any action.
        window_ids = run_xdotool("search", "--onlyvisible", "--name", ".").splitlines()
    except Exception:
        return []

    result: List[Tuple[str, str, str, Dict[str, int]]] = []
    for wid in window_ids:
        try:
            pid = int(run_xdotool("getwindowpid", wid))
            if current_uid is not None and proc_uid(pid) != current_uid:
                continue

            title = run_xdotool("getwindowname", wid)
            try:
                class_name = run_xdotool("getwindowclassname", wid)
            except Exception:
                class_name = ""
            if not looks_like_wechat(title, class_name):
                continue

            geometry = get_geometry(wid)
            if geometry.get("WIDTH", 0) < 300 or geometry.get("HEIGHT", 0) < 300:
                continue
            result.append((wid, title, class_name, geometry))
        except Exception:
            continue

    result.sort(
        key=lambda item: item[3].get("WIDTH", 0) * item[3].get("HEIGHT", 0),
        reverse=True,
    )
    return result


@contextlib.contextmanager
def display_lock() -> Iterator[None]:
    lock_dir = RUNTIME_DIR / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe_display = "".join(
        char if char.isalnum() or char in "_.-" else "_" for char in DISPLAY_NAME
    )
    lock_path = lock_dir / f"display-{safe_display or 'display'}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def find_account_window(account_id: str):
    print(f"Searching for WeChat window for account {account_id!r} on {DISPLAY_NAME}...")
    for _ in range(15):
        candidates = candidate_windows()
        if candidates:
            return candidates[0]
        time.sleep(1)
    return None


def run_auto_login(account_id: str) -> int:
    if not ENABLE_AUTO_LOGIN:
        print("WeChat auto-login disabled via ENABLE_WECHAT_AUTO_LOGIN=false.")
        return 0
    if not shutil_which("xdotool"):
        print("xdotool is unavailable; skipping auto-login safely.")
        return 0

    with display_lock():
        candidate = find_account_window(account_id)
        if candidate is None:
            print("WeChat account window not found within 15 seconds; no UI action taken.")
            return 0

        win_id, win_name, class_name, geom = candidate
        print(
            "Found account window "
            f"id={win_id} title={win_name!r} class={class_name!r} "
            f"size={geom.get('WIDTH')}x{geom.get('HEIGHT')}"
        )

        if win_name.strip().lower() in {"weixin", "wechat", "微信"}:
            print("Window appears to be the logged-in main screen; no login click needed.")
            return 0

        time.sleep(AUTO_LOGIN_DELAY)

        x = geom.get("X", 0)
        y = geom.get("Y", 0)
        width = geom.get("WIDTH", 0)
        height = geom.get("HEIGHT", 0)
        if width <= 0 or height <= 0:
            print("Window geometry is invalid; no UI action taken.")
            return 0

        # Analyze only the verified account window instead of the entire shared
        # desktop, otherwise another account's login button can cause a false
        # positive.
        image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
        pixels = list(
            image.get_flattened_data()
            if hasattr(image, "get_flattened_data")
            else image.getdata()
        )
        green_count = sum(
            1
            for pixel in pixels
            if len(pixel) >= 3
            and pixel[0] < 50
            and 150 <= pixel[1] <= 240
            and 60 <= pixel[2] <= 140
        )
        print(f"Account window login-color feature count: {green_count}")

        if green_count <= 1200:
            print(
                "QR/login confirmation appears to require manual action. "
                "Open the Selkies Web UI for this shared desktop."
            )
            return 0

        subprocess.check_call(
            ["xdotool", "windowactivate", "--sync", win_id], env=display_env()
        )
        time.sleep(0.5)
        subprocess.call(["xdotool", "key", "Return"], env=display_env())
        subprocess.call(
            [
                "xdotool",
                "mousemove",
                "--window",
                win_id,
                str(width // 2),
                str(int(height * 0.70)),
                "click",
                "1",
            ],
            env=display_env(),
        )
        print(f"Auto-login action dispatched for account {account_id!r}.")
        return 0


def shutil_which(command: str) -> Optional[str]:
    # Keep imports minimal because this helper is launched once per account.
    from shutil import which

    return which(command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--account",
        default=os.environ.get("WECHAT_ACCOUNT_ID", "default"),
        help="account id used for logging; Unix UID is the security boundary",
    )
    args = parser.parse_args()
    try:
        return run_auto_login(args.account)
    except Exception as exc:
        # Auto-login is convenience behavior. A failure must not kill the real
        # WeChat process or cause another account to be clicked as a fallback.
        print(f"Auto-login skipped after error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
