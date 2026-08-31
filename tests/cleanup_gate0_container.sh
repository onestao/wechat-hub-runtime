#!/bin/bash
set -euo pipefail

# Explicit cleanup for objects created by run_gate0_container.sh only.
# It never deletes images, volumes, account data, or arbitrary containers.

PROJECT_NAME="wechat-hub-a-gate0"
CONTAINER_NAME="wechat-hub-a-gate0-runtime"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "No $CONTAINER_NAME container exists; nothing to clean."
    exit 0
fi

ACTUAL_PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER_NAME")"
ACTUAL_MARKER="$(docker inspect -f '{{ index .Config.Labels "com.wechat-hub.gate0" }}' "$CONTAINER_NAME")"

if [ "$ACTUAL_PROJECT" != "$PROJECT_NAME" ] || [ "$ACTUAL_MARKER" != "session-a" ]; then
    echo "ERROR: safety labels do not match; refusing cleanup." >&2
    echo "project=$ACTUAL_PROJECT marker=$ACTUAL_MARKER" >&2
    exit 2
fi

docker compose \
    -p "$PROJECT_NAME" \
    -f docker-compose.yml \
    -f tests/docker-compose.gate0.yml \
    down --remove-orphans

echo "Removed only the isolated Gate-0 Compose container/network."
echo "Images and files were intentionally preserved."
