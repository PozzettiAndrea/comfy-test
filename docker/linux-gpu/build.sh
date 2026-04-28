#!/usr/bin/env bash
# Build the comfy-test Linux GPU image and run a smoke test.
# Requires: docker, nvidia-container-toolkit (for the smoke test).
#
# Usage:
#   ./build.sh                          # default: build + smoke test
#   ./build.sh --no-test                # build only
#   ./build.sh --tag my-tag:latest      # custom image tag

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="comfy-test-linux-gpu:full"
RUN_TEST=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)           IMAGE_TAG="$2"; shift 2 ;;
        --no-test)       RUN_TEST=false; shift ;;
        *)               echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

cp "$SCRIPT_DIR/Dockerfile" "$STAGE_DIR/Dockerfile"
cp "$SCRIPT_DIR/entrypoint.sh" "$STAGE_DIR/entrypoint.sh"

echo "Building $IMAGE_TAG ..."
docker build -t "$IMAGE_TAG" -f "$STAGE_DIR/Dockerfile" "$STAGE_DIR"

if [ $? -ne 0 ]; then
    echo "docker build failed" >&2
    exit 1
fi

if [ "$RUN_TEST" = true ]; then
    echo ""
    echo "=== Smoke test: comfy-test --help ==="
    docker run --rm "$IMAGE_TAG" --help
fi
