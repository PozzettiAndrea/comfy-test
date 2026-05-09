# Container entrypoint for comfy-test-windows-gpu:full.
#
# Installs comfy-test from PyPI at startup. If COMFY_TEST_NODE_URL is set,
# clones the node into C:\<name> first and changes into that directory
# before invoking the CLI.
#
# Set COMFY_TEST_VERSION (e.g. "0.2.46") to pin to a specific release;
# defaults to latest.

$ErrorActionPreference = 'Stop'
$env:PATH = "C:\Users\ContainerAdministrator\.local\bin;$env:PATH"

if ($env:COMFY_TEST_VERSION) {
    uv tool install --reinstall "comfy-test==$env:COMFY_TEST_VERSION"
} else {
    uv tool install --reinstall comfy-test
}

# URL mode: container does the clone (no host bind-mount of source).
if ($env:COMFY_TEST_NODE_URL) {
    $name = if ($env:COMFY_TEST_NODE_NAME) { $env:COMFY_TEST_NODE_NAME } else { 'node' }
    $dest = "C:\$name"
    $gitArgs = @()
    if ($env:COMFY_TEST_NODE_BRANCH) {
        $gitArgs += '--branch'
        $gitArgs += $env:COMFY_TEST_NODE_BRANCH
    }
    $branchDesc = if ($env:COMFY_TEST_NODE_BRANCH) { " (branch=$env:COMFY_TEST_NODE_BRANCH)" } else { "" }
    Write-Host "[entrypoint] cloning $env:COMFY_TEST_NODE_URL$branchDesc -> $dest"
    & git clone --depth 1 @gitArgs $env:COMFY_TEST_NODE_URL $dest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Set-Location $dest
}

& comfy-test @args
exit $LASTEXITCODE
