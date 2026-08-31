#!/bin/bash
set -euo pipefail

ACCOUNT_ID="${1:-${WECHAT_DEFAULT_ACCOUNT_ID:-default}}"
WINDOW_ID="$(/scripts/wechat/wechat-runtime window "$ACCOUNT_ID")"
exec /scripts/wechat/wechat-display-lock.sh "$ACCOUNT_ID" xdotool windowactivate --sync "$WINDOW_ID"
