# Build the Stage-1 spike image and run the decision-gate test.
# Requires Moby already installed and docker service running (see install-host.ps1).

param(
    [string]$SmbInstallers = '\\192.168.1.19\pxe\scripts\installers',
    [string]$StageDir      = 'D:\docker-stage\windows-gpu',
    [string]$NvidiaExe     = 'nvidia-driver-581.57.exe',
    [string]$GitExe        = 'Git-2.53.0-64-bit.exe',
    [ValidateSet('spike','full')] [string]$Target = 'spike',
    [string]$ImageTag      = ''
)

$ErrorActionPreference = 'Stop'
$env:DOCKER_BUILDKIT = '0'  # static Moby zip ships without buildx; legacy builder is fine for this Dockerfile

# Ensure Docker is on PATH (install-host.ps1 updates Machine PATH, but already-running shells won't see it until restart)
$dockerInstall = 'C:\Program Files\Docker'
if ((Test-Path (Join-Path $dockerInstall 'docker.exe')) -and ($env:Path -notlike "*$dockerInstall*")) {
    $env:Path = "$env:Path;$dockerInstall"
}

if (-not $ImageTag) { $ImageTag = "comfy-test-windows-gpu:$Target" }

New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

# Always stage the NVIDIA installer (both targets inherit from it via the spike image)
$nvSrc = Join-Path $SmbInstallers $NvidiaExe
$nvDst = Join-Path $StageDir $NvidiaExe
if (-not (Test-Path $nvDst)) {
    Write-Host "Copying $NvidiaExe from SMB staging -> $StageDir" -ForegroundColor Cyan
    Copy-Item -Path $nvSrc -Destination $nvDst
} else {
    Write-Host "NVIDIA installer already staged at $nvDst (skip copy)" -ForegroundColor Green
}

if ($Target -eq 'full') {
    # Stage Git installer
    $gitSrc = Join-Path $SmbInstallers $GitExe
    $gitDst = Join-Path $StageDir $GitExe
    if (-not (Test-Path $gitDst)) {
        Write-Host "Copying $GitExe from SMB staging -> $StageDir" -ForegroundColor Cyan
        Copy-Item -Path $gitSrc -Destination $gitDst
    } else {
        Write-Host "Git installer already staged at $gitDst (skip copy)" -ForegroundColor Green
    }

    # comfy-test is no longer baked into the image — it's installed from PyPI
    # at container start by entrypoint.ps1 (copied into the build context below).
    $entrypointSrc = Join-Path $PSScriptRoot 'entrypoint.ps1'
    $entrypointDst = Join-Path $StageDir 'entrypoint.ps1'
    Copy-Item -Force -Path $entrypointSrc -Destination $entrypointDst
    Write-Host "Staged entrypoint.ps1 -> $entrypointDst" -ForegroundColor Cyan
}

# Copy relevant Dockerfile into the staging dir
$dockerfileName = if ($Target -eq 'spike') { 'Dockerfile.spike' } else { 'Dockerfile' }
Copy-Item -Force -Path (Join-Path $PSScriptRoot $dockerfileName) -Destination (Join-Path $StageDir $dockerfileName)

Write-Host "Building $ImageTag ..." -ForegroundColor Cyan
docker build `
    --isolation=process `
    -t $ImageTag `
    -f (Join-Path $StageDir $dockerfileName) `
    $StageDir

if ($LASTEXITCODE -ne 0) {
    Write-Error "docker build failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

if ($Target -eq 'spike') {
    Write-Host ""
    Write-Host "=== Decision gate: CUDA inside Windows container ===" -ForegroundColor Yellow
    docker run --rm --isolation=process --device "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599" $ImageTag
} else {
    Write-Host ""
    Write-Host "=== Smoke test: comfy-test --help ===" -ForegroundColor Yellow
    docker run --rm --isolation=process --device "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599" $ImageTag --help
}
