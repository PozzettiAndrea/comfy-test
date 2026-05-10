"""`comfy-test vm build` -- one-time host setup for the baseline VM.

After this command:
  - Hyper-V is enabled (reboot may be required first time)
  - A Generation 2 VM exists with Windows 11 mounted as install media
  - The host's NVIDIA GPU is detached + attached to the VM via DDA
  - VM firmware is configured for first-boot from the ISO

The user then completes the IN-VM checklist (printed at end), takes a
snapshot via `comfy-test vm snapshot`, and dispatch-test.yml's
windows-desktop-gpu job uses `comfy-test vm restore` to revert per-test.

Run as Administrator (Hyper-V cmdlets + DDA both require it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ._root import (
    DEFAULT_VIRTUAL_SWITCH,
    DEFAULT_VM_CPU,
    DEFAULT_VM_DISK_GB,
    DEFAULT_VM_MEMORY_GB,
    DEFAULT_VM_NAME,
    DEFAULT_VM_ROOT,
    _ps,
    _require_windows,
    _vm_exists,
)


_GUEST_CHECKLIST = """\
[vm] HOST setup complete. NEXT (manual, in the VM):

  1. Connect to the VM:
       vmconnect.exe localhost {vm_name}
     OR via Hyper-V Manager.

  2. Install Windows 11 from the mounted ISO. Create a LOCAL user
     "ci-runner" (any strong password). Sign in.

  3. Install the NVIDIA driver INSIDE the VM (the host's DDA-attached
     GPU appears in Device Manager as needs-driver):
       https://www.nvidia.com/Download/index.aspx

  4. Install ComfyUI Desktop:
       Invoke-WebRequest "https://download.comfy.org/windows/nsis/x64" `
                         -OutFile "$env:TEMP\\ComfyUI-Setup.exe"
       Start-Process "$env:TEMP\\ComfyUI-Setup.exe" -ArgumentList "/S" -Wait

  5. Install Python 3.12 + comfy-test:
       (download python-3.12.x amd64 .exe, install with PrependPath=1)
       pip install --upgrade comfy-test uv

  6. Configure autologon (so VM boots straight to a desktop session --
     REQUIRED for any Electron app to launch):
       $key = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"
       Set-ItemProperty $key "AutoAdminLogon"  "1"
       Set-ItemProperty $key "DefaultUserName" "ci-runner"
       Set-ItemProperty $key "DefaultPassword" "<the password>"

  7. Apply the Defender + IO tweaks the dispatch YML applies on hosted
     runners (mirror the "Tune Windows runner for IO-heavy installs" step
     from .github/workflows/dispatch-test.yml).

  8. Register the GHA runner UNDER the autologon user (NOT SYSTEM --
     SYSTEM is Session 0 and chromium can't open windows there):
       https://github.com/<org>/<repo>/settings/actions/runners/new
       (pick Windows x64; copy URL+token; in C:\\actions-runner:)
       .\\config.cmd --url <URL> --token <TOKEN> `
                     --labels self-hosted,windows,cuda,desktop `
                     --runasservice `
                     --windowslogonaccount ci-runner `
                     --windowslogonpassword <password>

  9. From the HOST, snapshot the clean state:
       comfy-test vm snapshot --name clean-baseline

 10. dispatch-test.yml's windows-desktop-gpu job calls
       comfy-test vm restore --snapshot clean-baseline
     before each run. Per-test isolation done.
"""


def cmd_vm_build(args) -> int:
    _require_windows()

    vm_name = args.vm_name
    vm_root = Path(args.vm_root)

    # ---------------------------------------------------------------------
    # 1. Hyper-V
    # ---------------------------------------------------------------------
    print("[vm] checking Hyper-V")
    r = _ps(
        "(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All).State",
        capture=True,
    )
    state = (r.stdout or "").strip()
    if state != "Enabled":
        print("[vm] enabling Hyper-V (reboot required after this)")
        _ps("Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart")
        print("[vm] Hyper-V queued for enable. REBOOT and re-run `comfy-test vm build`.")
        return 0
    print(f"[vm] Hyper-V: {state}")

    # ---------------------------------------------------------------------
    # 2. VM creation
    # ---------------------------------------------------------------------
    if _vm_exists(vm_name):
        print(f"[vm] VM '{vm_name}' already exists; skipping create")
    else:
        print(f"[vm] creating VM '{vm_name}' at {vm_root}")
        vm_root.mkdir(parents=True, exist_ok=True)
        vhd = vm_root / f"{vm_name}.vhdx"
        _ps(f"New-VHD -Path '{vhd}' -SizeBytes {args.disk_gb}GB -Dynamic | Out-Null")
        _ps(
            f"New-VM -Name '{vm_name}' -Generation 2 "
            f"-MemoryStartupBytes {args.memory_gb}GB "
            f"-VHDPath '{vhd}' -SwitchName '{args.switch}' -Path '{vm_root}' | Out-Null"
        )
        _ps(
            f"Set-VMProcessor -VMName '{vm_name}' -Count {args.cpu} "
            f"-ExposeVirtualizationExtensions $true; "
            f"Set-VMMemory -VMName '{vm_name}' -DynamicMemoryEnabled $false; "
            # DDA prerequisites
            f"Set-VM -Name '{vm_name}' -AutomaticStopAction TurnOff "
            f"-CheckpointType Disabled; "
            f"Set-VM -Name '{vm_name}' -GuestControlledCacheTypes $true; "
            f"Set-VM -Name '{vm_name}' -LowMemoryMappedIoSpace 3GB "
            f"-HighMemoryMappedIoSpace 33GB"
        )

    # ---------------------------------------------------------------------
    # 3. GPU DDA passthrough
    # ---------------------------------------------------------------------
    if args.skip_dda:
        print("[vm] --skip-dda: not touching GPU passthrough")
    else:
        print(f"[vm] identifying NVIDIA GPU (filter: '{args.gpu_filter}')")
        r = _ps(
            "Get-PnpDevice -Class Display -PresentOnly | "
            f"Where-Object {{ $_.FriendlyName -like '*{args.gpu_filter}*' }} | "
            "Select-Object -First 1 -ExpandProperty InstanceId",
            capture=True,
        )
        gpu_id = (r.stdout or "").strip()
        if not gpu_id:
            print(f"[vm] no GPU matched filter '{args.gpu_filter}'. "
                  f"Available display devices:", file=sys.stderr)
            _ps("Get-PnpDevice -Class Display -PresentOnly | "
                "Format-Table FriendlyName,InstanceId -AutoSize")
            return 1
        print(f"[vm] GPU InstanceId: {gpu_id}")

        # WARNING: this detaches the GPU from the host. Confirm before doing it.
        print("[vm] *** about to dismount the GPU from the host ***")
        print("[vm] *** the host loses display until VM stops + GPU re-mounted ***")
        if not args.yes:
            ans = input("[vm] type 'yes' to proceed: ").strip().lower()
            if ans != "yes":
                print("[vm] aborted")
                return 1

        loc_r = _ps(
            f"(Get-PnpDeviceProperty -InstanceId '{gpu_id}' "
            f"-KeyName DEVPKEY_Device_LocationPaths).Data[0]",
            capture=True,
        )
        loc = (loc_r.stdout or "").strip()
        print(f"[vm] LocationPath: {loc}")

        _ps(f"Disable-PnpDevice -InstanceId '{gpu_id}' -Confirm:$false")
        _ps(f"Dismount-VMHostAssignableDevice -LocationPath '{loc}' -Force")
        _ps(f"Add-VMAssignableDevice -VMName '{vm_name}' -LocationPath '{loc}'")
        print("[vm] GPU now attached to VM via DDA")

    # ---------------------------------------------------------------------
    # 4. Mount Windows ISO if provided
    # ---------------------------------------------------------------------
    if args.iso:
        iso = Path(args.iso).resolve()
        if not iso.exists():
            print(f"[vm] ISO not found at {iso}", file=sys.stderr)
            return 1
        print(f"[vm] mounting ISO {iso} as first-boot device")
        _ps(f"Add-VMDvdDrive -VMName '{vm_name}' -Path '{iso}'")
        _ps(
            f"Set-VMFirmware -VMName '{vm_name}' "
            f"-FirstBootDevice (Get-VMDvdDrive -VMName '{vm_name}')"
        )
    else:
        print("[vm] no --iso passed; skipping install-media mount")
        print("[vm]   (you can re-run with --iso PATH or attach manually)")

    # ---------------------------------------------------------------------
    # 5. Done
    # ---------------------------------------------------------------------
    print(_GUEST_CHECKLIST.format(vm_name=vm_name))
    return 0


def add_vm_build_parser(subparsers):
    p = subparsers.add_parser(
        "build",
        help="One-time host setup: enable Hyper-V, create baseline VM, "
             "DDA-attach the GPU. Run as Administrator.",
    )
    p.add_argument("--vm-name", default=DEFAULT_VM_NAME,
                   help=f"VM name (default: {DEFAULT_VM_NAME})")
    p.add_argument("--vm-root", default=str(DEFAULT_VM_ROOT),
                   help=f"VM dir on disk (default: {DEFAULT_VM_ROOT})")
    p.add_argument("--cpu", type=int, default=DEFAULT_VM_CPU,
                   help=f"vCPU count (default: {DEFAULT_VM_CPU})")
    p.add_argument("--memory-gb", type=int, default=DEFAULT_VM_MEMORY_GB,
                   help=f"Static RAM in GB (default: {DEFAULT_VM_MEMORY_GB})")
    p.add_argument("--disk-gb", type=int, default=DEFAULT_VM_DISK_GB,
                   help=f"Dynamic VHDX max size in GB (default: {DEFAULT_VM_DISK_GB})")
    p.add_argument("--switch", default=DEFAULT_VIRTUAL_SWITCH,
                   help=f"Hyper-V virtual switch (default: '{DEFAULT_VIRTUAL_SWITCH}')")
    p.add_argument("--iso", default=None, metavar="PATH",
                   help="Optional Windows ISO to mount as first-boot media")
    p.add_argument("--gpu-filter", default="NVIDIA",
                   help="Substring to match against display device FriendlyName "
                        "for DDA passthrough (default: 'NVIDIA')")
    p.add_argument("--skip-dda", action="store_true",
                   help="Skip GPU passthrough (e.g. for testing the VM "
                        "creation flow without committing the host's GPU)")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the interactive confirm before dismounting GPU "
                        "from host")
    p.set_defaults(func=cmd_vm_build)
