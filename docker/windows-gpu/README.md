# comfy-test on Windows + Docker + NVIDIA GPU

Process-isolated Windows container that runs `comfy-test` with CUDA passthrough on an NVIDIA GPU. Unofficial path — relies on `--device class/5B45201D-F2F2-4F3B-85BB-30FF1F953599` (GUID_DEVINTERFACE_DISPLAY_ADAPTER) and an exact host/container NVIDIA driver match.

## Host prerequisites

- Windows 11 Pro (client SKU works for process isolation; Hyper-V not required)
- NVIDIA driver installed on host — take note of the exact version
- Matching driver installer staged in the build context (see below)
- Admin PowerShell for the install step

## One-time host setup

Run **elevated**:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-host.ps1
```

Phase 1 enables the `Containers` optional feature. **Reboot.** Re-run the script; phase 2 downloads the latest Moby static binaries, registers `dockerd` as a Windows service, and starts it.

## Build the Stage-1 spike

```powershell
.\build.ps1
```

This copies `nvidia-driver-581.57.exe` from `\\192.168.1.19\pxe\scripts\installers\` into `D:\docker-stage\windows-gpu\`, builds `comfy-test-windows-gpu:spike`, and immediately runs the decision-gate test.

**Expected output** on a working setup:

```
torch 2.x.x+cu128
cuda? True
NVIDIA GeForce RTX 5060
```

If `cuda?` is `False` → CUDA didn't initialize in the container; inspect `C:\Windows\System32\nvcuda.dll` inside the container and check driver install logs.

If the container errors on start with `code 43` → ABI mismatch between host kernel driver and in-container user-mode driver. Verify the installer version matches `nvidia-smi --query-gpu=driver_version`.

## What comes next

Stage 2 builds the full `comfy-test` image on top of the spike base. Stage 3 wires a `--docker` flag into `cds test`.
