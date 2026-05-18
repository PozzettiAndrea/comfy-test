# Container entrypoint for comfy-test-windows-gpu:full.
#
# Installs comfy-test from PyPI at startup. If COMFY_TEST_NODE_URL is set,
# clones the node into C:\<name> first and changes into that directory
# before invoking the CLI.
#
# Set COMFY_TEST_VERSION (e.g. "0.2.46") to pin to a specific release;
# defaults to latest.

$ErrorActionPreference = 'Stop'
$env:PATH = "C:\Users\ContainerUser\.local\bin;$env:PATH"
# Diagnostic: surface the runtime identity at every container start so a
# regression to admin (e.g. someone adds `--user ContainerAdministrator` at
# `docker run` time) is visible in CI logs at the top of the log.
Write-Host "[entrypoint] running as $env:USERNAME"

# Plumb GitHub auth into git globally so any `git clone https://github.com/...`
# from a node's install.py picks up the token transparently. Without this,
# install.py subclones of private dependency repos (e.g. ComfyUI-MoGe2)
# silently fail to authenticate. Mirrors cli/_git_auth.authenticated_github_url
# but as a git-config rewrite so it intercepts every subprocess.
$ghToken = if ($env:NODE_PAT) { $env:NODE_PAT } `
    elseif ($env:GH_TOKEN) { $env:GH_TOKEN } `
    elseif ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } else { $null }
if ($ghToken) {
    & git config --global url."https://x-access-token:$ghToken@github.com/".insteadOf "https://github.com/" 2>$null
    Write-Host "[entrypoint] git auth: github.com PAT installed (rewrite)"
} else {
    Write-Host "[entrypoint] git auth: no PAT in env (public clones only)"
}

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
