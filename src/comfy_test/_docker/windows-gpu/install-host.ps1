# Host prep for process-isolated Windows containers with NVIDIA GPU passthrough.
#
# Runs in two phases; re-run the script after each reboot.
#   Phase 1: enable the 'Containers' Windows feature (needs reboot)
#   Phase 2: install the static Moby (docker) binaries and register dockerd as a service
#
# Requires: elevated PowerShell (Run as Administrator).

$ErrorActionPreference = 'Stop'

$logDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = Join-Path $logDir 'install-host.log'
Start-Transcript -Path $logFile -Force -IncludeInvocationHeader | Out-Null

function Assert-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Run this script from an elevated PowerShell (Run as Administrator)."
        exit 1
    }
}

Assert-Admin

$containers = Get-WindowsOptionalFeature -Online -FeatureName Containers
if ($containers.State -ne 'Enabled') {
    Write-Host "[phase 1] Enabling 'Containers' Windows optional feature..." -ForegroundColor Cyan
    Enable-WindowsOptionalFeature -Online -FeatureName Containers -All -NoRestart | Out-Null
    Write-Host ""
    Write-Host "Containers feature enabled. REBOOT the machine, then re-run this script." -ForegroundColor Yellow
    Write-Host "  shutdown /r /t 0" -ForegroundColor Yellow
    exit 0
}

Write-Host "[phase 2] 'Containers' feature already enabled." -ForegroundColor Green

# Moby (Docker CE) static Windows binaries
$installRoot = 'C:\Program Files\Docker'
$dockerExe   = Join-Path $installRoot 'docker.exe'
$dockerdExe  = Join-Path $installRoot 'dockerd.exe'

if (Test-Path $dockerdExe) {
    Write-Host "Docker binaries already present at $installRoot" -ForegroundColor Green
} else {
    # Pick latest stable zip from https://download.docker.com/win/static/stable/x86_64/
    Write-Host "Listing available stable releases..." -ForegroundColor Cyan
    $index = Invoke-WebRequest -UseBasicParsing 'https://download.docker.com/win/static/stable/x86_64/'
    $latest = ($index.Links |
        Where-Object { $_.href -match '^docker-(\d+\.\d+\.\d+)\.zip$' } |
        ForEach-Object { [PSCustomObject]@{ ver = [version]$Matches[1]; href = $_.href } } |
        Sort-Object ver -Descending |
        Select-Object -First 1)
    if (-not $latest) { Write-Error "Could not parse Moby release index."; exit 1 }
    $zipUrl = "https://download.docker.com/win/static/stable/x86_64/$($latest.href)"
    $zipDst = Join-Path $env:TEMP $latest.href
    Write-Host "Downloading $zipUrl ..." -ForegroundColor Cyan
    Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zipDst

    Write-Host "Extracting to $installRoot ..." -ForegroundColor Cyan
    if (Test-Path $installRoot) { Remove-Item -Recurse -Force $installRoot }
    Expand-Archive -Path $zipDst -DestinationPath 'C:\Program Files' -Force
    Remove-Item $zipDst

    # Add to system PATH (machine scope)
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($machinePath -notlike "*$installRoot*") {
        [Environment]::SetEnvironmentVariable('Path', "$machinePath;$installRoot", 'Machine')
        Write-Host "Added $installRoot to system PATH (new shells will see it)." -ForegroundColor Green
    }
    $env:Path = "$env:Path;$installRoot"
}

# Register dockerd as a Windows service if not already
$svc = Get-Service -Name docker -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "Registering dockerd as a Windows service..." -ForegroundColor Cyan
    & $dockerdExe --register-service --service-name docker
}

Write-Host "Starting docker service..." -ForegroundColor Cyan
Start-Service docker
(Get-Service docker) | Format-Table -AutoSize

Write-Host ""
Write-Host "Smoke-test:" -ForegroundColor Cyan
& $dockerExe version
& $dockerExe info --format '{{.OSType}} isolation={{.Isolation}} server={{.ServerVersion}}'

Write-Host ""
Write-Host "Done. Open a NEW shell so the updated PATH takes effect, then run build.ps1." -ForegroundColor Green
