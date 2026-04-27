# Container entrypoint for comfy-test-windows-gpu:full.
#
# Installs comfy-test from PyPI at startup, then forwards args to the CLI.
# Set COMFY_TEST_VERSION (e.g. "0.2.46") to pin to a specific release;
# defaults to latest.

$ErrorActionPreference = 'Stop'
$env:PATH = "C:\Users\ContainerAdministrator\.local\bin;$env:PATH"

if ($env:COMFY_TEST_VERSION) {
    uv tool install --reinstall "comfy-test==$env:COMFY_TEST_VERSION"
} else {
    uv tool install --reinstall comfy-test
}

& comfy-test @args
exit $LASTEXITCODE
