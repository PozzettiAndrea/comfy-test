#!/bin/bash
# Container entrypoint for comfy-test-linux-gpu:full.
#
# Installs comfy-test from PyPI at startup. If COMFY_TEST_NODE_URL is set,
# clones the node into /node/<name> first and changes into that directory
# before invoking the CLI.
#
# Set COMFY_TEST_VERSION (e.g. "0.3.5") to pin to a specific release;
# defaults to latest.

set -e

if [ -n "$COMFY_TEST_VERSION" ]; then
    uv tool install --reinstall --python 3.10 "comfy-test==$COMFY_TEST_VERSION"
else
    uv tool install --reinstall --python 3.10 comfy-test
fi

# URL mode: container does the clone (no host bind-mount of source).
if [ -n "$COMFY_TEST_NODE_URL" ]; then
    name="${COMFY_TEST_NODE_NAME:-node}"
    dest="/node/$name"
    mkdir -p /node
    git_args=()
    [ -n "$COMFY_TEST_NODE_BRANCH" ] && git_args+=(--branch "$COMFY_TEST_NODE_BRANCH")
    echo "[entrypoint] cloning $COMFY_TEST_NODE_URL${COMFY_TEST_NODE_BRANCH:+ (branch=$COMFY_TEST_NODE_BRANCH)} -> $dest"
    git clone --depth 1 "${git_args[@]}" "$COMFY_TEST_NODE_URL" "$dest"
    cd "$dest"
fi

exec comfy-test "$@"
