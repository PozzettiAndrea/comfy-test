# comfy-test on Windows + Docker + NVIDIA GPU

Process-isolated Windows container that runs `comfy-test` with CUDA passthrough on an NVIDIA GPU. Unofficial path — relies on `--device class/5B45201D-F2F2-4F3B-85BB-30FF1F953599` (GUID_DEVINTERFACE_DISPLAY_ADAPTER).

> ### ⚠ The container's NVIDIA driver MUST match the host's exactly
>
> Process-isolated Windows containers share the host's kernel-mode NVIDIA driver via the `class/5B45201D-...` device passthrough. The user-mode driver baked into the image (the one `NVIDIA_INSTALLER` installs in stage 1 of the Dockerfile) **must be the identical version**.
>
> - Mismatch → `code 43` at `torch.cuda.init()`, container-start hangs, or `cuda? False`.
> - On every host driver upgrade, **rebuild the container** with the matching `nvidia-driver-<version>.exe` staged in `\\192.168.1.19\pxe\scripts\installers\`.
> - `build.ps1` queries the host driver via `nvidia-smi --query-gpu=driver_version` and refuses to build if no matching installer is staged.

## Host prerequisites

- Windows 11 Pro (client SKU works for process isolation; Hyper-V not required)
- NVIDIA driver installed on host
- Matching `nvidia-driver-<host-version>.exe` staged in `\\192.168.1.19\pxe\scripts\installers\`
- Admin PowerShell for the install step

## One-time host setup

Run **elevated**:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-host.ps1
```

Phase 1 enables the `Containers` optional feature. **Reboot.** Re-run the script; phase 2 downloads the latest Moby static binaries, registers `dockerd` as a Windows service, and starts it.

## Build

```powershell
.\build.ps1
```

The script:
1. Queries the host driver via `nvidia-smi`, expects `nvidia-driver-<version>.exe` staged in `\\192.168.1.19\pxe\scripts\installers\`. Fails fast if not found (override with `-NvidiaExe`).
2. Stages driver + Git installer + Dockerfile + entrypoint into `D:\docker-stage\windows-gpu\`.
3. `docker build --isolation=process` produces `comfy-test-windows-gpu:full`.
4. **Smoke test 1**: spins up a throwaway uv venv inside the image, `uv pip install torch ... cu128`, runs `torch.cuda.is_available()`. Expected:
   ```
   torch 2.x.x+cu128
   cuda? True
   NVIDIA GeForce RTX <model>
   ```
   Note the `torch` install happens at smoke-test time only — the image itself does not bake torch (kept lean; comfy-test installs torch per-test via uv).
5. **Smoke test 2**: `docker run --rm <image> --help` — confirms the entrypoint installs comfy-test from PyPI and the CLI is reachable.

If `cuda? False` or `code 43`: ABI mismatch between host kernel driver and in-container user-mode driver. Re-run with `-NvidiaExe nvidia-driver-<correct-version>.exe`. The driver-version guard at the start of `build.ps1` should normally catch this before the build, but there have been cases where multiple installers are staged with the wrong filename.

## Roll-out across the cluster

Each Windows runner loads `comfy-test-windows-gpu:full` from the SMB share at PXE-install time (`setup-<user>.bat` step 9). To deploy a new image cluster-wide:

```powershell
# On the build host (e.g. andrew — the 4060 Ti, less in-demand than 3090s):
.\build.ps1
docker save comfy-test-windows-gpu:full | zstd -19 -o comfy-test-windows-gpu-full.tar.zst

# Push to SMB share (overwrites previous):
Copy-Item comfy-test-windows-gpu-full.tar.zst `
    \\192.168.1.19\pxe\scripts\comfy-test-windows-gpu-full.tar.zst
```

After this:
- The build host has the new image locally and uses it on next CI run.
- Other workstations keep their old image until they reimage (which auto-loads from SMB) or run a manual `docker load` against the SMB tarball.
- Future PXE installs pick up the new image automatically.

This is a one-runner-builds, multi-runner-rollout pattern. The build host pays the build cost (~15-25 min); everyone else gets the cached binary.

## Why `windows/server` and not `servercore`?

`mcr.microsoft.com/windows/server:ltsc2025` keeps Media Foundation (`mfplat.dll`). `windows/servercore` strips it. ComfyUI imports PyAV at startup, and PyAV loads `mfplat.dll`. Switching to servercore would shave ~1.5 GB but introduces a fragile manual Media-Foundation re-install step. Not worth it until image size becomes a real bottleneck.

## Why no digest pin?

We use the floating `:ltsc2025` tag and accept that monthly Patch Tuesday cumulative updates change the base image bytes. The driver-match guard in `build.ps1` catches the only failure mode that actually matters (driver version skew). Pinning to a digest forces manual quarterly bumps for marginal benefit.

## Files

- `Dockerfile` — single-stage build for `comfy-test-windows-gpu:full`.
- `Dockerfile.spike.archive` — old stage-1 CUDA-feasibility test (kept for reference; not used by `build.ps1`).
- `build.ps1` — staging + build + smoke + rollout instructions.
- `entrypoint.ps1` — installs comfy-test from PyPI at container start, then exec's the CLI.
- `install-host.ps1` — one-time host bootstrap (Containers feature + Moby).
