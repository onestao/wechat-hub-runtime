#!/bin/bash
set -euo pipefail

# Non-destructive host-side Gate-0 launcher for a shared Linux Docker host.
# It refuses to replace, remove, stop, or reuse any pre-existing container,
# Compose project, or listening port.

PROJECT_NAME="wechat-hub-a-gate0"
CONTAINER_NAME="wechat-hub-a-gate0-runtime"
HTTP_PORT="${GATE0_HTTP_PORT:-13000}"
HTTPS_PORT="${GATE0_HTTPS_PORT:-13001}"
ACCOUNT_A="${GATE0_ACCOUNT_A:-gate0-a}"
ACCOUNT_B="${GATE0_ACCOUNT_B:-gate0-b}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() {
    echo "ERROR: $*" >&2
    exit 2
}

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is unavailable"
[ -f docker-compose.yml ] || fail "docker-compose.yml not found in $ROOT_DIR"
[ -f tests/docker-compose.gate0.yml ] || fail "Gate-0 compose override is missing"
[ -e /dev/dri ] || fail "/dev/dri is unavailable; do not change the shared host to work around this"

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    fail "container $CONTAINER_NAME already exists; refusing to replace or remove it"
fi

EXISTING_PROJECT="$(docker ps -a \
    --filter "label=com.docker.compose.project=$PROJECT_NAME" \
    --format '{{.Names}}' 2>/dev/null || true)"
if [ -n "$EXISTING_PROJECT" ]; then
    fail "Compose project $PROJECT_NAME already exists ($EXISTING_PROJECT); refusing to reuse it"
fi

port_is_busy() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        if ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"; then
            return 0
        fi
    fi
    if docker ps --format '{{.Ports}}' 2>/dev/null | grep -Eq "(^|, )([^,]*:)?${port}->"; then
        return 0
    fi
    return 1
}

port_is_busy "$HTTP_PORT" && fail "host port $HTTP_PORT is already in use"
port_is_busy "$HTTPS_PORT" && fail "host port $HTTPS_PORT is already in use"

export HTTP_PORT HTTPS_PORT
export AUTO_START_WECHAT=false
export ENABLE_WECHAT_NIGHTLY_RESTART=false
export ENABLE_WECHAT_AUTO_LOGIN=false
export WECHAT_ACCOUNTS="$ACCOUNT_A,$ACCOUNT_B"
export WECHAT_DEFAULT_ACCOUNT_ID="$ACCOUNT_A"

COMPOSE=(docker compose -p "$PROJECT_NAME" -f docker-compose.yml -f tests/docker-compose.gate0.yml)

echo "Building isolated Gate-0 image..."
"${COMPOSE[@]}" build

echo "Starting isolated Gate-0 container..."
"${COMPOSE[@]}" up -d --no-build

ACTUAL_PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER_NAME")"
ACTUAL_MARKER="$(docker inspect -f '{{ index .Config.Labels "com.wechat-hub.gate0" }}' "$CONTAINER_NAME")"
[ "$ACTUAL_PROJECT" = "$PROJECT_NAME" ] || fail "unexpected Compose project label: $ACTUAL_PROJECT"
[ "$ACTUAL_MARKER" = "session-a" ] || fail "Gate-0 safety marker is missing"

echo "Waiting for the Selkies desktop bootstrap..."
sleep "${GATE0_BOOT_SECONDS:-8}"

echo "Running same-DISPLAY two-account process proof..."
docker exec \
    -e WECHAT_POC_SETTLE_SECONDS="${WECHAT_POC_SETTLE_SECONDS:-8}" \
    "$CONTAINER_NAME" \
    /scripts/wechat/poc_same_display.sh "$ACCOUNT_A" "$ACCOUNT_B"

echo
echo "Runtime health:"
docker exec "$CONTAINER_NAME" /scripts/wechat/wechat-runtime health --json || true

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Gate-0 container is intentionally left running for real WeChat login proof."
echo "HTTP : http://${HOST_IP:-HOST}:$HTTP_PORT"
echo "HTTPS: https://${HOST_IP:-HOST}:$HTTPS_PORT"
echo "Cleanup is explicit and guarded: tests/cleanup_gate0_container.sh"
