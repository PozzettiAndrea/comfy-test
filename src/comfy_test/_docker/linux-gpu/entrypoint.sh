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

# Plumb GitHub auth into git globally so any `git clone https://github.com/...`
# from a node's install.py picks up the token transparently. Without this,
# install.py subclones of private dependency repos silently fail to
# authenticate. Mirrors cli/_git_auth.authenticated_github_url but as a
# git-config rewrite so it intercepts every subprocess.
gh_token="${NODE_PAT:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
if [ -n "$gh_token" ]; then
    git config --global url."https://x-access-token:${gh_token}@github.com/".insteadOf "https://github.com/"
    echo "[entrypoint] git auth: github.com PAT installed (rewrite)"
else
    echo "[entrypoint] git auth: no PAT in env (public clones only)"
fi

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
