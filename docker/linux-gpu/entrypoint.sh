#!/bin/bash
# Container entrypoint for comfy-test-linux-gpu:full.
#
# Installs comfy-test from PyPI at startup, then forwards args to the CLI.
# Set COMFY_TEST_VERSION (e.g. "0.3.5") to pin to a specific release;
# defaults to latest.

set -e

if [ -n "$COMFY_TEST_VERSION" ]; then
    uv tool install --reinstall --python 3.10 "comfy-test==$COMFY_TEST_VERSION"
else
    uv tool install --reinstall --python 3.10 comfy-test
fi

exec comfy-test "$@"
