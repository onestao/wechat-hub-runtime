#!/bin/bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <account_id> <command> [args...]" >&2
    exit 2
fi

ACCOUNT_ID="$1"
shift

DISPLAY_NAME="$(/scripts/wechat/wechat-runtime display "$ACCOUNT_ID")"
SAFE_DISPLAY="$(printf '%s' "$DISPLAY_NAME" | tr -c 'A-Za-z0-9_.-' '_')"
RUNTIME_DIR="${WECHAT_RUNTIME_DIR:-/run/wechat-runtime}"
LOCK_DIR="$RUNTIME_DIR/locks"
mkdir -p "$LOCK_DIR"

# Clipboard, focus and xdotool-style input are display-global even when each
# WeChat process has a separate Unix user/HOME/XDG tree.
exec flock "$LOCK_DIR/display-${SAFE_DISPLAY}.lock" "$@"
