"""`comfy-test vm build` -- one-time host setup for the baseline VM.

Two modes:

* **Manual (default)** -- enables Hyper-V, creates the Gen 2 VM, DDA-attaches
  the GPU, optionally mounts a Windows ISO, then prints the in-VM checklist.
  The user finishes the install + post-install steps by hand and snapshots.

* **Unattended (`--unattended`)** -- additionally generates an answer ISO
  (`autounattend.xml` + `post-install.ps1`) under `vm_root/setup.iso`,
  attaches it as a second DVD, enables vTPM, starts the VM, and waits for
  the VM to power itself off (the post-install script's success signal).
  Then detaches the answer ISO and snapshots `clean-baseline`. Walk-away.

Run as Administrator (Hyper-V cmdlets + DDA both require it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ._root import (
    DEFAULT_SNAPSHOT_NAME,
    DEFAULT_VIRTUAL_SWITCH,
    DEFAULT_VM_CPU,
    DEFAULT_VM_DISK_GB,
    DEFAULT_VM_MEMORY_GB,
    DEFAULT_VM_NAME,
    DEFAULT_VM_ROOT,
    _ps,
    _require_windows,
    _vm_exists,
    _wait_for_vm_state,
)


# Defaults that are easy to bump if the upstream URL/version moves.
DEFAULT_PYTHON_URL = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
DEFAULT_COMFY_DESKTOP_URL = "https://download.comfy.org/windows/nsis/x64"
DEFAULT_RUNNER_VERSION = "2.319.1"
DEFAULT_WINDOWS_EDITION = "Windows 11 Pro"
DEFAULT_PROVISION_TIMEOUT = 4 * 3600  # 4h


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

  8. Register the GHA runner so it runs in ci-runner's INTERACTIVE
     desktop session -- NOT as a service. Services run in Session 0,
     no interactive desktop, chromium / Electron / ComfyUI Desktop
     break:
       https://github.com/<org>/<repo>/settings/actions/runners/new
       (pick Windows x64; copy URL+token; in C:\\actions-runner:)
       .\\config.cmd --url <URL> --token <TOKEN> `
                     --labels self-hosted,windows,cuda,desktop `
                     --unattended
       # Drop a Startup-folder shortcut so run.cmd launches on autologon:
       $startup = [Environment]::GetFolderPath("Startup")
       $wsh = New-Object -ComObject WScript.Shell
       $lnk = $wsh.CreateShortcut("$startup\\GHA Runner.lnk")
       $lnk.TargetPath       = "C:\\actions-runner\\run.cmd"
       $lnk.WorkingDirectory = "C:\\actions-runner"
       $lnk.Save()

  9. From the HOST, snapshot the clean state:
       comfy-test vm snapshot --name clean-baseline

 10. dispatch-test.yml's windows-desktop-gpu job calls
       comfy-test vm restore --snapshot clean-baseline
     before each run. Per-test isolation done.

(Or skip steps 2-9 entirely with `comfy-test vm build --unattended ...`.)
"""


def _resolve_secret(value, env_var, *, what):
    """Return value if present, else read os.environ[env_var]. None if neither."""
    if value:
        return value
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v
    return None


def _validate_unattended_args(args) -> dict:
    """Returns a dict of resolved values; exits non-zero on missing required ones."""
    missing = []

    password = _resolve_secret(args.password, args.password_env, what="password")
    if not password:
        missing.append("--password / --password-env")

    runner_token = _resolve_secret(
        args.runner_token, args.runner_token_env, what="runner-token"
    )
    if not runner_token:
        missing.append("--runner-token / --runner-token-env")

    if not args.runner_url:
        missing.append("--runner-url")
    if not args.nvidia_driver_url:
        missing.append("--nvidia-driver-url")
    if not args.iso:
        missing.append("--iso (required for --unattended)")

    if missing:
        print(f"[vm] --unattended needs: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    return {
        "password":          password,
        "runner_url":        args.runner_url,
        "runner_token":      runner_token,
        "nvidia_driver_url": args.nvidia_driver_url,
        "python_url":        args.python_url,
        "comfy_desktop_url": args.comfy_desktop_url,
        "runner_version":    args.runner_version,
        "win_edition":       args.windows_edition,
        "computer_name":     args.vm_name.upper().replace("-", "")[:15],
        "timezone":          args.timezone,
    }


def _attach_iso(vm_name: str, iso_path: Path) -> None:
    _ps(f"Add-VMDvdDrive -VMName '{vm_name}' -Path '{iso_path}'")


def _set_first_boot_to_dvd_with_path(vm_name: str, iso_path: Path) -> None:
    _ps(
        f"Set-VMFirmware -VMName '{vm_name}' -FirstBootDevice "
        f"(Get-VMDvdDrive -VMName '{vm_name}' | "
        f"Where-Object {{ $_.Path -eq '{iso_path}' }} | Select-Object -First 1)"
    )


def _detach_iso(vm_name: str, iso_path: Path) -> None:
    _ps(
        f"Get-VMDvdDrive -VMName '{vm_name}' | "
        f"Where-Object {{ $_.Path -eq '{iso_path}' }} | "
        f"Remove-VMDvdDrive"
    )


def _enable_vtpm(vm_name: str) -> None:
    # Local key protector is per-VM; safe to set even if one already exists.
    _ps(
        f"if (-not (Get-VMKeyProtector -VMName '{vm_name}')) "
        f"{{ Set-VMKeyProtector -VMName '{vm_name}' -NewLocalKeyProtector }}; "
        f"Enable-VMTPM -VMName '{vm_name}'"
    )


def _provision_unattended(args, windows_iso: Path) -> int:
    """Generate setup.iso, attach + start VM, wait for shutdown, snapshot."""
    # Imported here so non-Windows (e.g. CI lint runs) can still import build.
    from . import _unattend

    resolved = _validate_unattended_args(args)
    vm_name = args.vm_name
    vm_root = Path(args.vm_root)

    print("[vm] enabling vTPM (required for Win11 unattended install)")
    _enable_vtpm(vm_name)

    print(f"[vm] generating answer ISO at {vm_root / 'setup.iso'}")
    params = _unattend.Params(
        password=resolved["password"],
        runner_url=resolved["runner_url"],
        runner_token=resolved["runner_token"],
        nvidia_driver_url=resolved["nvidia_driver_url"],
        python_url=resolved["python_url"],
        comfy_desktop_url=resolved["comfy_desktop_url"],
        runner_version=resolved["runner_version"],
        win_edition=resolved["win_edition"],
        computer_name=resolved["computer_name"],
        timezone=resolved["timezone"],
    )
    setup_iso = _unattend.build_setup_iso(vm_root, params)

    print(f"[vm] attaching {setup_iso} as second DVD")
    _attach_iso(vm_name, setup_iso)

    # Re-pin first-boot to the Windows ISO (the Windows ISO drive id is
    # already first by insertion order, but be explicit).
    _set_first_boot_to_dvd_with_path(vm_name, windows_iso)

    print(f"[vm] starting VM '{vm_name}'")
    _ps(f"Start-VM -Name '{vm_name}'")

    print(f"[vm] provisioning will take ~30-60 min (Windows install + reboots).")
    print(f"[vm] watching VM state; will detect shutdown as success signal")
    print(f"[vm] (timeout: {args.provision_timeout}s; tail "
          f"`vmconnect.exe localhost {vm_name}` to watch)")

    last_state = ""
    def _tick(state):
        nonlocal last_state
        if state != last_state:
            print(f"[vm]   state: {state}")
            last_state = state

    ok = _wait_for_vm_state(
        vm_name, target="Off",
        timeout_seconds=args.provision_timeout,
        poll_seconds=15,
        on_tick=_tick,
    )
    if not ok:
        print(f"[vm] timed out waiting for VM '{vm_name}' to power off.",
              file=sys.stderr)
        print(f"[vm] connect with `vmconnect.exe localhost {vm_name}` and "
              f"check C:\\comfy-setup\\setup.log for which stage stalled.",
              file=sys.stderr)
        return 1

    print(f"[vm] VM is Off; detaching answer ISO (keeps secrets out of the snapshot)")
    _detach_iso(vm_name, setup_iso)

    if args.no_snapshot:
        print(f"[vm] --no-snapshot: skipping snapshot. "
              f"Run `comfy-test vm snapshot` manually when ready.")
    else:
        snap = DEFAULT_SNAPSHOT_NAME
        print(f"[vm] checkpointing as '{snap}'")
        _ps(f"Checkpoint-VM -Name '{vm_name}' -SnapshotName '{snap}'")
        print(f"[vm] snapshot created. "
              f"Use `comfy-test vm restore --start` to revert + boot.")

    print(f"[vm] DONE. The GHA runner should appear online a few seconds "
          f"after the next boot.")
    return 0


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
    windows_iso: Path | None = None
    if args.iso:
        windows_iso = Path(args.iso).resolve()
        if not windows_iso.exists():
            print(f"[vm] ISO not found at {windows_iso}", file=sys.stderr)
            return 1
        print(f"[vm] mounting ISO {windows_iso} as first-boot device")
        _attach_iso(vm_name, windows_iso)
        _set_first_boot_to_dvd_with_path(vm_name, windows_iso)
    else:
        print("[vm] no --iso passed; skipping install-media mount")
        print("[vm]   (you can re-run with --iso PATH or attach manually)")

    # ---------------------------------------------------------------------
    # 5. Branch: unattended provisioning vs. manual checklist
    # ---------------------------------------------------------------------
    if args.unattended:
        if windows_iso is None:
            # Already handled by _validate_unattended_args, but defensive.
            print("[vm] --unattended requires --iso", file=sys.stderr)
            return 2
        return _provision_unattended(args, windows_iso)

    print(_GUEST_CHECKLIST.format(vm_name=vm_name))
    return 0


def add_vm_build_parser(subparsers):
    p = subparsers.add_parser(
        "build",
        help="One-time host setup: enable Hyper-V, create baseline VM, "
             "DDA-attach the GPU. Add --unattended for fully automated "
             "Windows install + post-install. Run as Administrator.",
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
                   help="Windows ISO to mount as first-boot media. "
                        "Required for --unattended.")
    p.add_argument("--gpu-filter", default="NVIDIA",
                   help="Substring to match against display device FriendlyName "
                        "for DDA passthrough (default: 'NVIDIA')")
    p.add_argument("--skip-dda", action="store_true",
                   help="Skip GPU passthrough (e.g. for testing the VM "
                        "creation flow without committing the host's GPU)")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip the interactive confirm before dismounting GPU "
                        "from host")

    # Unattended-mode flags
    g = p.add_argument_group("unattended provisioning")
    g.add_argument("--unattended", action="store_true",
                   help="After host setup, generate an answer ISO + run the "
                        "Windows install + post-install (NVIDIA driver, "
                        "Python, comfy-test, ComfyUI Desktop, autologon, "
                        "GHA runner) end-to-end, then snapshot. "
                        "Requires --iso, --password[-env], --runner-url, "
                        "--runner-token[-env], --nvidia-driver-url.")
    g.add_argument("--password", default=None, metavar="TEXT",
                   help="Local 'ci-runner' password. Prefer --password-env "
                        "to keep secrets out of shell history.")
    g.add_argument("--password-env", default=None, metavar="VAR",
                   help="Read 'ci-runner' password from this env var.")
    g.add_argument("--runner-url", default=None, metavar="URL",
                   help="GHA runner registration URL "
                        "(https://github.com/<org>/<repo>).")
    g.add_argument("--runner-token", default=None, metavar="TOKEN",
                   help="GHA runner registration token. Expires ~1h after "
                        "generation; grab a fresh one from the repo's runner "
                        "page right before running. Prefer --runner-token-env.")
    g.add_argument("--runner-token-env", default=None, metavar="VAR",
                   help="Read GHA runner token from this env var.")
    g.add_argument("--nvidia-driver-url", default=None, metavar="URL",
                   help="Direct download URL for the NVIDIA driver .exe "
                        "(look up the matching driver for your GPU on "
                        "nvidia.com once and pass it in).")
    g.add_argument("--python-url", default=DEFAULT_PYTHON_URL, metavar="URL",
                   help=f"Python installer URL (default: pinned 3.12.7 amd64)")
    g.add_argument("--comfy-desktop-url", default=DEFAULT_COMFY_DESKTOP_URL,
                   metavar="URL",
                   help=f"ComfyUI Desktop installer URL "
                        f"(default: {DEFAULT_COMFY_DESKTOP_URL})")
    g.add_argument("--runner-version", default=DEFAULT_RUNNER_VERSION,
                   metavar="VER",
                   help=f"actions/runner release version "
                        f"(default: {DEFAULT_RUNNER_VERSION})")
    g.add_argument("--windows-edition", default=DEFAULT_WINDOWS_EDITION,
                   metavar="NAME",
                   help=f"Image name in the Windows ISO to install "
                        f"(default: '{DEFAULT_WINDOWS_EDITION}')")
    g.add_argument("--timezone", default="UTC", metavar="TZ",
                   help="Windows time zone (default: UTC)")
    g.add_argument("--provision-timeout", type=int,
                   default=DEFAULT_PROVISION_TIMEOUT, metavar="SECONDS",
                   help=f"Max seconds to wait for the VM to power off after "
                        f"unattended install (default: {DEFAULT_PROVISION_TIMEOUT})")
    g.add_argument("--no-snapshot", action="store_true",
                   help="Skip the auto-snapshot after unattended provisioning "
                        "(debug; run `comfy-test vm snapshot` manually)")

    p.set_defaults(func=cmd_vm_build)
