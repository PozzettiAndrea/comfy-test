# Build the comfy-test Windows GPU image and run smoke tests.
# Requires Moby already installed and docker service running (see install-host.ps1).
#
# Single-stage build: produces comfy-test-windows-gpu:full directly from
# windows/server:ltsc2025. The old two-stage spike→full split has been folded
# into one Dockerfile.

param(
    [string]$SmbInstallers = '\\192.168.1.19\pxe\scripts\installers',
    [string]$StageDir      = 'D:\docker-stage\windows-gpu',
    [string]$NvidiaExe     = '',
    [string]$GitExe        = 'Git-2.53.0-64-bit.exe',
    [string]$ImageTag      = 'comfy-test-windows-gpu:full',
    [switch]$NoSmoke
)

$ErrorActionPreference = 'Stop'
$env:DOCKER_BUILDKIT = '0'  # static Moby zip ships without buildx; legacy builder is fine

# Ensure Docker is on PATH (install-host.ps1 updates Machine PATH, but already-running shells won't see it until restart).
$dockerInstall = 'C:\Program Files\Docker'
if ((Test-Path (Join-Path $dockerInstall 'docker.exe')) -and ($env:Path -notlike "*$dockerInstall*")) {
    $env:Path = "$env:Path;$dockerInstall"
}

# --- Driver-version guard ---------------------------------------------------
# Process-isolated Windows containers share the host's kernel-mode NVIDIA
# driver. The container's user-mode driver MUST match the host's bit-for-bit
# or torch.cuda.init() fails with code 43. Query the host driver and require
# a matching nvidia-driver-<version>.exe to be staged in $SmbInstallers.
if (-not $NvidiaExe) {
    $hostDriver = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1).Trim()
    if (-not $hostDriver) {
        Write-Error "Could not query host NVIDIA driver via nvidia-smi. Install the driver on the host first, or pass -NvidiaExe explicitly."
        exit 2
    }
    $NvidiaExe = "nvidia-driver-$hostDriver.exe"
    Write-Host "Host driver detected: $hostDriver -> requires $NvidiaExe" -ForegroundColor Cyan
}

$nvSrc = Join-Path $SmbInstallers $NvidiaExe
if (-not (Test-Path $nvSrc)) {
    Write-Error "Driver installer not found at $nvSrc. Container driver must match host driver exactly. Stage $NvidiaExe in $SmbInstallers (or pass -NvidiaExe matching a staged file) and retry."
    exit 3
}

# --- Stage build context ---------------------------------------------------
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

# NVIDIA installer
$nvDst = Join-Path $StageDir $NvidiaExe
if (-not (Test-Path $nvDst)) {
    Write-Host "Copying $NvidiaExe from SMB staging -> $StageDir" -ForegroundColor Cyan
    Copy-Item -Path $nvSrc -Destination $nvDst
} else {
    Write-Host "NVIDIA installer already staged at $nvDst (skip copy)" -ForegroundColor Green
}

# Git installer
$gitSrc = Join-Path $SmbInstallers $GitExe
$gitDst = Join-Path $StageDir $GitExe
if (-not (Test-Path $gitDst)) {
    Write-Host "Copying $GitExe from SMB staging -> $StageDir" -ForegroundColor Cyan
    Copy-Item -Path $gitSrc -Destination $gitDst
} else {
    Write-Host "Git installer already staged at $gitDst (skip copy)" -ForegroundColor Green
}

# Dockerfile + entrypoint
Copy-Item -Force -Path (Join-Path $PSScriptRoot 'Dockerfile') -Destination (Join-Path $StageDir 'Dockerfile')
Copy-Item -Force -Path (Join-Path $PSScriptRoot 'entrypoint.ps1') -Destination (Join-Path $StageDir 'entrypoint.ps1')
Write-Host "Staged Dockerfile + entrypoint.ps1 -> $StageDir" -ForegroundColor Cyan

# --- Build -----------------------------------------------------------------
# Stamp the comfy-test commit + tag the image was built from. Read by the
# home-cluster dashboard's "Runner Docker Images" table.
$gitRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$gitCommit = (& git -C $gitRoot rev-parse --short HEAD 2>$null)
if (-not $gitCommit) { $gitCommit = 'unknown' }
$gitTag = (& git -C $gitRoot describe --tags --abbrev=0 2>$null)
if (-not $gitTag) { $gitTag = 'unknown' }

Write-Host "Building $ImageTag (comfy_test_version=$gitTag comfy_test_commit=$gitCommit) ..." -ForegroundColor Cyan
docker build `
    --isolation=process `
    --build-arg NVIDIA_INSTALLER=$NvidiaExe `
    --build-arg GIT_INSTALLER=$GitExe `
    --label "comfy_test_commit=$gitCommit" `
    --label "comfy_test_version=$gitTag" `
    -t $ImageTag `
    -f (Join-Path $StageDir 'Dockerfile') `
    $StageDir

if ($LASTEXITCODE -ne 0) {
    Write-Error "docker build failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

# --- Smoke tests -----------------------------------------------------------
if ($NoSmoke) { exit 0 }

Write-Host ""
Write-Host "=== CUDA gate: torch.cuda.is_available() inside the image ===" -ForegroundColor Yellow
# Install torch into a throwaway uv-managed venv inside the container, just
# for the smoke check. Doesn't bake into the image — the run uses --rm.
$cudaCmd = @'
$ErrorActionPreference='Stop';
$env:PATH = "C:\Users\ContainerAdministrator\.local\bin;$env:PATH";
uv venv --python 3.10 C:\smoke-venv | Out-Null;
& C:\smoke-venv\Scripts\activate.ps1;
uv pip install --no-cache torch --index-url https://download.pytorch.org/whl/cu128 | Out-Null;
python -c "import torch; print('torch', torch.__version__); print('cuda?', torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU')"
'@
docker run --rm --isolation=process --device "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599" `
    --entrypoint "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    $ImageTag -NoProfile -ExecutionPolicy Bypass -Command $cudaCmd

if ($LASTEXITCODE -ne 0) {
    Write-Error "CUDA smoke test failed (exit $LASTEXITCODE) — host/container driver mismatch?"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Entrypoint smoke: comfy-test --help ===" -ForegroundColor Yellow
docker run --rm --isolation=process --device "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599" $ImageTag --help

if ($LASTEXITCODE -ne 0) {
    Write-Error "entrypoint smoke failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Image $ImageTag built and smoke-tested successfully." -ForegroundColor Green
Write-Host "To roll out cluster-wide: docker save $ImageTag | zstd -19 -o comfy-test-windows-gpu-full.tar.zst, then push to \\\\192.168.1.19\\pxe\\scripts\\." -ForegroundColor Cyan
