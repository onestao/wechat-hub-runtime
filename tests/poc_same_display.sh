#!/bin/bash
set -euo pipefail

# Gate-0 topology A proof harness. Run INSIDE the built Selkies container after
# the desktop/X11 session is available. It intentionally does not delete users
# or account data after the test.

ACCOUNT_A="${1:-gate0-a}"
ACCOUNT_B="${2:-gate0-b}"
DISPLAY_NAME="${DISPLAY:-:1}"
RUNTIME="/scripts/wechat/wechat-runtime"

ensure_account() {
    local account_id="$1"
    if ! "$RUNTIME" status "$account_id" --json >/dev/null 2>&1; then
        "$RUNTIME" register "$account_id" --display "$DISPLAY_NAME" --json
    fi
}

ensure_account "$ACCOUNT_A"
ensure_account "$ACCOUNT_B"

"$RUNTIME" start "$ACCOUNT_A" --json
"$RUNTIME" start "$ACCOUNT_B" --json
sleep "${WECHAT_POC_SETTLE_SECONDS:-5}"

TMP_A="$(mktemp)"
TMP_B="$(mktemp)"
trap 'rm -f "$TMP_A" "$TMP_B"' EXIT

"$RUNTIME" status "$ACCOUNT_A" --json >"$TMP_A"
"$RUNTIME" status "$ACCOUNT_B" --json >"$TMP_B"

/lsiopy/bin/python3 - "$TMP_A" "$TMP_B" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    a = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    b = json.load(handle)

errors = []
if not a.get("running"):
    errors.append(f"{a['account_id']} has no discovered WeChat process")
if not b.get("running"):
    errors.append(f"{b['account_id']} has no discovered WeChat process")
if a.get("uid") == b.get("uid"):
    errors.append("accounts do not have distinct Unix UIDs")
if a.get("display") != b.get("display"):
    errors.append("accounts are not using the same DISPLAY for topology A")
if a.get("home") == b.get("home"):
    errors.append("accounts do not have distinct HOME directories")

print(json.dumps({"account_a": a, "account_b": b, "errors": errors}, indent=2, ensure_ascii=False))
if errors:
    raise SystemExit(1)
PY

echo
echo "Process-level topology A proof passed. Complete the real Gate-0 proof by"
echo "opening the Selkies UI, logging both official WeChat clients in, and"
echo "capturing the resulting status JSON/window IDs in docs/GATE0_POC_RESULT.md."

