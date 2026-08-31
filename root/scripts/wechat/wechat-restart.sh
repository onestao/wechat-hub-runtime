#!/bin/bash
set -euo pipefail

ACCOUNT_ID="${1:-${WECHAT_DEFAULT_ACCOUNT_ID:-default}}"
exec /scripts/wechat/wechat-runtime restart "$ACCOUNT_ID"