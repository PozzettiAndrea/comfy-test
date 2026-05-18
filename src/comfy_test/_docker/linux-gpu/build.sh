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

# Stamp the comfy-test commit + tag the image was built from. Read by the
# home-cluster dashboard's "Runner Docker Images" table to surface scaffolding
# version (comfy-test itself is installed from PyPI at container start, so the
# label tracks the Dockerfile/entrypoint/system-deps generation, not runtime).
GIT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GIT_COMMIT="$(git -C "$GIT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_TAG="$(git -C "$GIT_ROOT" describe --tags --abbrev=0 2>/dev/null || echo unknown)"

echo "Building $IMAGE_TAG (comfy_test_version=$GIT_TAG comfy_test_commit=$GIT_COMMIT) ..."
docker build \
    --label "comfy_test_commit=$GIT_COMMIT" \
    --label "comfy_test_version=$GIT_TAG" \
    -t "$IMAGE_TAG" \
    -f "$STAGE_DIR/Dockerfile" \
    "$STAGE_DIR"

if [ $? -ne 0 ]; then
    echo "docker build failed" >&2
    exit 1
fi

if [ "$RUN_TEST" = true ]; then
    echo ""
    echo "=== Smoke test: comfy-test --help ==="
    # --user inherits the caller's uid so the smoke test exercises the same
    # unprivileged path as `comfy-test docker run` (see _run_linux). Without
    # it the container runs as root and would silently paper over any
    # permission misconfiguration introduced into the image.
    docker run --rm --user "$(id -u):$(id -g)" "$IMAGE_TAG" --help
fi
