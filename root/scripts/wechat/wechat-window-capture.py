#!/usr/bin/env python3
"""Capture one explicit X11 window as an in-memory PNG for login UI.

The helper is executed as the target WeChat account Unix user.  It never
writes the image to disk and only accepts an already account-scoped window ID
selected by the privileged Runtime control service.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time

from PIL import Image, ImageGrab


def geometry_for(window_id: str) -> dict[str, int]:
    output = subprocess.check_output(
        ["xdotool", "getwindowgeometry", "--shell", window_id],
        stderr=subprocess.DEVNULL,
    ).decode("utf-8", errors="replace")
    result: dict[str, int] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in {"X", "Y", "WIDTH", "HEIGHT"}:
            continue
        try:
            result[key] = int(value)
        except ValueError:
            continue
    if result.get("WIDTH", 0) <= 0 or result.get("HEIGHT", 0) <= 0:
        raise RuntimeError("WeChat window geometry is unavailable")
    return result


def capture(window_id: str, *, max_width: int, max_height: int) -> bytes:
    # ImageGrab reads the visible X11 framebuffer. Bring the already
    # account-scoped window to the front first so a peer account cannot cover
    # the requested QR/login surface.
    subprocess.check_call(
        ["xdotool", "windowactivate", "--sync", window_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.call(
        ["xdotool", "windowraise", window_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.12)
    geom = geometry_for(window_id)
    x = geom.get("X", 0)
    y = geom.get("Y", 0)
    width = geom["WIDTH"]
    height = geom["HEIGHT"]
    image = ImageGrab.grab(
        bbox=(x, y, x + width, y + height),
        xdisplay=os.environ.get("DISPLAY") or ":1",
    ).convert("RGB")
    if image.width > max_width or image.height > max_height:
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=6)
    return output.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-id", required=True)
    parser.add_argument("--max-width", type=int, default=960)
    parser.add_argument("--max-height", type=int, default=820)
    args = parser.parse_args(argv)
    content = capture(
        args.window_id,
        max_width=max(320, min(args.max_width, 1600)),
        max_height=max(320, min(args.max_height, 1400)),
    )
    sys.stdout.buffer.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
