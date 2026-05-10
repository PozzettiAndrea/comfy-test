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
import secrets
import string
import sys
from pathlib import Path
from typing import Optional

from ._iso import (
    DEFAULT_WIN_ARCH,
    DEFAULT_WIN_LANG,
    DEFAULT_WIN_RELEASE,
    download_windows_iso,
)
from ._root import (
    DEFAULT_GPU_FILTER,
    DEFAULT_NVIDIA_DRIVER_VERSION,
    DEFAULT_SHARE_NAME,
    DEFAULT_SHARED_FOLDER,
    DEFAULT_SNAPSHOT_NAME,
    DEFAULT_VIRTUAL_SWITCH,
    DEFAULT_VM_CPU,
    DEFAULT_VM_DISK_GB,
    DEFAULT_VM_MEMORY_GB,
    DEFAULT_VM_NAME,
    DEFAULT_VM_ROOT,
    _default_switch_host_ip,
    _gpu_attached_to_vm,
    _gpu_in_host_limbo,
    _gpu_location_path_for_instance,
    _gpu_on_host,
    _ps,
    _require_windows,
    _resolve_nvidia_driver_url,
    _smb_share_exists,
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

  9. (Optional) Set up the host->VM shared folder for persistent files
     (model weights, outputs -- survives snapshot-restores):
       comfy-test vm share create
       # then inside the VM, mount as Z::
       New-PSDrive -Name Z -PSProvider FileSystem `
                   -Root '\\\\<host-ip>\\ComfySharedVM' `
                   -Persist -Credential (Get-Credential)

 10. From the HOST, snapshot the clean state:
       comfy-test vm snapshot --name clean-baseline

 11. dispatch-test.yml's windows-desktop-gpu job calls
       comfy-test vm restore --snapshot clean-baseline
     before each run. Per-test isolation done.

 12. To reclaim the GPU for your workstation:
       comfy-test vm gpu detach
     ...and to give it back to the VM:
       comfy-test vm gpu attach

(Or skip steps 2-9 entirely with `comfy-test vm build --unattended ...`,
which auto-downloads the Windows ISO, mounts it, runs Windows install +
post-install end-to-end, sets up the SMB share, and snapshots.)
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


def _generate_password(length: int = 20) -> str:
    """Strong password for the local 'ci-runner' account. Avoids ambiguous
    characters (0/O, 1/l/I) so the user can read it back from a printed
    banner without confusion."""
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"   # no I, O
        "abcdefghijkmnpqrstuvwxyz"   # no l, o
        "23456789"                   # no 0, 1
        "!@#%&*-_+="
    )
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _validate_unattended_args(args) -> dict:
    """Returns a dict of resolved values; exits non-zero on missing requirements.

    Nothing is strictly required anymore -- `--unattended` with no other
    flags will auto-resolve everything:
      * password   -- generated and printed (silenceable with --password[-env])
      * driver URL -- queried from nvidia-smi, falls back to a pinned version
      * ISO        -- auto-downloaded via Fido in cmd_vm_build
      * runner     -- skipped if --runner-url / --runner-token-env not provided
                      (unless --require-runner is set)
    """
    # --- Password: generate if not provided.
    password = _resolve_secret(args.password, args.password_env, what="password")
    password_was_generated = False
    if not password:
        password = _generate_password()
        password_was_generated = True

    # --- NVIDIA driver URL: auto-resolve.
    nvidia_url: Optional[str] = args.nvidia_driver_url
    if not nvidia_url:
        try:
            nvidia_url = _resolve_nvidia_driver_url(
                explicit_url=None,
                explicit_version=args.nvidia_driver_version,
            )
        except RuntimeError as e:
            print(f"[vm] {e}", file=sys.stderr)
            sys.exit(2)

    # --- GHA runner: optional. Only validate if at least one runner flag
    # was provided OR --require-runner is set.
    runner_token = _resolve_secret(
        args.runner_token, args.runner_token_env, what="runner-token"
    )
    skip_runner = args.no_runner or (
        not args.require_runner and not args.runner_url and not runner_token
    )

    runner_missing = []
    if not skip_runner:
        if not runner_token:
            runner_missing.append("--runner-token / --runner-token-env")
        if not args.runner_url:
            runner_missing.append("--runner-url")
    if runner_missing:
        print(f"[vm] runner setup needs: {', '.join(runner_missing)}\n"
              f"     (or pass --no-runner to skip runner registration)",
              file=sys.stderr)
        sys.exit(2)

    if password_was_generated:
        print()
        print("=" * 64)
        print("[vm] GENERATED PASSWORD for in-VM 'ci-runner' account:")
        print(f"     {password}")
        print("[vm] SAVE THIS NOW. You'll need it to log into the VM,")
        print("     or pass --password-env to silence this banner.")
        print("=" * 64)
        print()

    return {
        "password":           password,
        "password_generated": password_was_generated,
        "runner_url":         args.runner_url    if not skip_runner else "",
        "runner_token":       runner_token       if not skip_runner else "",
        "skip_runner":        skip_runner,
        "nvidia_driver_url":  nvidia_url,
        "python_url":         args.python_url,
        "comfy_desktop_url":  args.comfy_desktop_url,
        "runner_version":     args.runner_version,
        "win_edition":        args.windows_edition,
        "computer_name":      args.vm_name.upper().replace("-", "")[:15],
        "timezone":           args.timezone,
    }


def _ensure_share_for_vm(args, password: str) -> Optional[str]:
    """If --shared-folder is requested, ensure the host SMB share exists and
    return the UNC the VM should mount (or None if disabled)."""
    if args.no_shared_folder:
        return None

    folder = Path(args.shared_folder).resolve()
    name = args.shared_folder_name

    print(f"[vm] ensuring shared folder {folder} (SMB share '{name}')")
    folder.mkdir(parents=True, exist_ok=True)

    if _smb_share_exists(name):
        print(f"[vm]   SMB share '{name}' already exists; reusing")
    else:
        # FullAccess needs the local account to exist. The unattended flow
        # creates 'ci-runner' on first boot; until then, grant Everyone
        # (we replace this with a tighter ACL once ci-runner exists --
        # post-install can re-run New-SmbShare scoped to ci-runner).
        # For simplicity: grant the user we'll create. New-SmbShare accepts
        # account names that don't yet exist (resolved at access time).
        print(f"[vm]   creating SMB share '{name}' -> {folder} "
              f"(full access for 'ci-runner')")
        _ps(
            f"New-SmbShare -Name '{name}' -Path '{folder}' "
            f"-FullAccess 'ci-runner' "
            f"-Description 'comfy-test VM persistent storage'",
            check=False,
        )

    ip = _default_switch_host_ip()
    if not ip:
        print(f"[vm]   couldn't read 'vEthernet (Default Switch)' IP; "
              f"skipping share UNC", file=sys.stderr)
        return None
    return f"\\\\{ip}\\{name}"


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


def _provision_unattended(args, windows_iso: Path, resolved: dict,
                          shared_unc: Optional[str]) -> int:
    """Generate setup.iso, attach + start VM, wait for shutdown, snapshot."""
    # Imported here so non-Windows (e.g. CI lint runs) can still import build.
    from . import _unattend

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
        shared_folder_unc=shared_unc,
        shared_folder_user="ci-runner" if shared_unc else None,
        shared_folder_password=resolved["password"] if shared_unc else None,
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
    # 0. Fail-fast validation BEFORE any side-effects.
    # ---------------------------------------------------------------------
    # This used to live inside _provision_unattended (called at step 5),
    # which meant a missing --password / --runner-url etc. only surfaced
    # AFTER we'd already enabled Hyper-V, created the VM, and DDA'd the
    # GPU. Now we check up front so the user can re-run safely.
    resolved: Optional[dict] = None
    if args.unattended:
        resolved = _validate_unattended_args(args)

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
    # 3. GPU DDA passthrough (idempotent)
    # ---------------------------------------------------------------------
    if args.skip_dda:
        print("[vm] --skip-dda: not touching GPU passthrough")
    else:
        already = _gpu_attached_to_vm(vm_name, args.gpu_filter)
        if already:
            print(f"[vm] GPU already attached to VM '{vm_name}' at {already}; "
                  f"skipping passthrough setup")
        else:
            limbo = _gpu_in_host_limbo(args.gpu_filter)
            if limbo:
                # Host already dismounted it but never assigned. Common after
                # a previous half-finished build run.
                print(f"[vm] GPU is host-detached at {limbo}; assigning to "
                      f"VM '{vm_name}'")
                _ps(f"Add-VMAssignableDevice -VMName '{vm_name}' "
                    f"-LocationPath '{limbo}'")
                print(f"[vm] GPU now attached to VM via DDA")
            else:
                instance_id = _gpu_on_host(args.gpu_filter)
                if not instance_id:
                    print(f"[vm] no GPU matching filter '{args.gpu_filter}' "
                          f"on host or in host-detached limbo. Available "
                          f"display devices:", file=sys.stderr)
                    _ps("Get-PnpDevice -Class Display -PresentOnly | "
                        "Format-Table FriendlyName,InstanceId -AutoSize")
                    print(f"\n[vm] tip: pass --gpu-filter <substring> to match "
                          f"a different vendor (default: "
                          f"'{DEFAULT_GPU_FILTER}'), or --skip-dda to build "
                          f"the VM without GPU passthrough.", file=sys.stderr)
                    return 1
                print(f"[vm] GPU InstanceId: {instance_id}")

                location = _gpu_location_path_for_instance(instance_id)
                if not location:
                    print(f"[vm] couldn't read DEVPKEY_Device_LocationPaths "
                          f"for {instance_id}", file=sys.stderr)
                    return 1
                print(f"[vm] LocationPath: {location}")

                # WARNING: this detaches the GPU from the host. Confirm.
                print("[vm] *** about to dismount the GPU from the host ***")
                print("[vm] *** the host loses display until the VM is shut "
                      "down AND `comfy-test vm gpu detach` runs ***")
                if not args.yes:
                    ans = input("[vm] type 'yes' to proceed: ").strip().lower()
                    if ans != "yes":
                        print("[vm] aborted")
                        return 1

                _ps(f"Disable-PnpDevice -InstanceId '{instance_id}' -Confirm:$false")
                _ps(f"Dismount-VMHostAssignableDevice -LocationPath '{location}' -Force")
                _ps(f"Add-VMAssignableDevice -VMName '{vm_name}' "
                    f"-LocationPath '{location}'")
                print("[vm] GPU now attached to VM via DDA")

    # ---------------------------------------------------------------------
    # 4. Resolve / download Windows ISO
    # ---------------------------------------------------------------------
    windows_iso: Optional[Path] = None
    if args.iso:
        windows_iso = Path(args.iso).resolve()
        if not windows_iso.exists():
            print(f"[vm] ISO not found at {windows_iso}", file=sys.stderr)
            return 1
        print(f"[vm] using provided ISO {windows_iso}")
    elif args.unattended and not args.no_iso_download:
        # Auto-download (cached under vm_root/iso/Win<release>_<lang>_<arch>.iso).
        print(f"[vm] no --iso provided; auto-downloading via Fido")
        windows_iso = download_windows_iso(
            vm_root,
            release=args.iso_release,
            lang=args.iso_lang,
            arch=args.iso_arch,
        )
    elif args.unattended:
        print("[vm] --unattended needs an ISO. Pass --iso PATH or remove "
              "--no-iso-download to auto-fetch.", file=sys.stderr)
        return 2

    if windows_iso is not None:
        # Idempotency: if the ISO is already attached, don't double-attach.
        # Get-VMDvdDrive -VMName | Where Path -eq <iso>
        r = _ps(
            f"if (Get-VMDvdDrive -VMName '{vm_name}' | "
            f"  Where-Object {{ $_.Path -eq '{windows_iso}' }}) "
            f"{{ 'yes' }} else {{ 'no' }}",
            capture=True, check=False,
        )
        if (r.stdout or "").strip() == "yes":
            print(f"[vm] ISO already attached to VM; skipping mount")
        else:
            print(f"[vm] mounting ISO {windows_iso} as first-boot device")
            _attach_iso(vm_name, windows_iso)
            _set_first_boot_to_dvd_with_path(vm_name, windows_iso)
    else:
        print("[vm] no ISO; skipping install-media mount")
        print("[vm]   (you can re-run with --iso PATH or attach manually)")

    # ---------------------------------------------------------------------
    # 5. Optional shared folder (host -> VM SMB share)
    # ---------------------------------------------------------------------
    shared_unc: Optional[str] = None
    if args.unattended:
        shared_unc = _ensure_share_for_vm(args, resolved["password"] if resolved else "")

    # ---------------------------------------------------------------------
    # 6. Branch: unattended provisioning vs. manual checklist
    # ---------------------------------------------------------------------
    if args.unattended:
        assert resolved is not None  # validated at step 0
        return _provision_unattended(args, windows_iso, resolved, shared_unc)

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
                        "If omitted in --unattended mode, auto-downloads via "
                        "Fido (cached under <vm-root>/iso/).")
    p.add_argument("--gpu-filter", default=DEFAULT_GPU_FILTER,
                   help=f"GPU vendor filter; matches PCI vendor for "
                        f"nvidia/amd/intel or a literal substring otherwise "
                        f"(default: '{DEFAULT_GPU_FILTER}')")
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
                        "optional GHA runner) end-to-end, then snapshot. "
                        "Everything auto-resolves with sensible defaults: "
                        "ISO via Fido, password generated, NVIDIA driver via "
                        "nvidia-smi (or pinned fallback), runner registration "
                        "skipped unless --runner-url + --runner-token-env are "
                        "passed.")
    g.add_argument("--password", default=None, metavar="TEXT",
                   help="Local 'ci-runner' password. If omitted, a strong "
                        "random password is generated and printed once. "
                        "Prefer --password-env to keep secrets out of shell "
                        "history.")
    g.add_argument("--password-env", default=None, metavar="VAR",
                   help="Read 'ci-runner' password from this env var.")
    g.add_argument("--runner-url", default=None, metavar="URL",
                   help="GHA runner registration URL "
                        "(https://github.com/<org>/<repo>). If omitted, "
                        "runner registration is skipped (configure manually "
                        "later or re-run with --require-runner to fail "
                        "fast on missing flags).")
    g.add_argument("--runner-token", default=None, metavar="TOKEN",
                   help="GHA runner registration token. Expires ~1h after "
                        "generation; grab a fresh one from the repo's runner "
                        "page right before running. Prefer --runner-token-env.")
    g.add_argument("--runner-token-env", default=None, metavar="VAR",
                   help="Read GHA runner token from this env var.")
    g.add_argument("--no-runner", action="store_true",
                   help="Force-skip GHA runner registration even if runner "
                        "flags are provided.")
    g.add_argument("--require-runner", action="store_true",
                   help="Fail the build if runner flags are missing instead "
                        "of silently skipping registration.")
    g.add_argument("--nvidia-driver-url", default=None, metavar="URL",
                   help="Direct download URL for the NVIDIA driver .exe. If "
                        "omitted, auto-resolved from --nvidia-driver-version "
                        "or nvidia-smi or the pinned fallback "
                        f"({DEFAULT_NVIDIA_DRIVER_VERSION}).")
    g.add_argument("--nvidia-driver-version", default=None, metavar="VER",
                   help="NVIDIA driver version to fetch (e.g. '581.57'). "
                        "Used when --nvidia-driver-url isn't given AND "
                        "nvidia-smi can't be queried (e.g. host's GPU is "
                        "DDA'd to the VM). Default: probe nvidia-smi, else "
                        f"{DEFAULT_NVIDIA_DRIVER_VERSION}.")
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

    # Auto-ISO-download flags
    iso_g = p.add_argument_group("Windows ISO download (when --iso not provided)")
    iso_g.add_argument("--iso-release", default=DEFAULT_WIN_RELEASE, metavar="VER",
                      help=f"Windows release passed to Fido "
                           f"(default: '{DEFAULT_WIN_RELEASE}')")
    iso_g.add_argument("--iso-lang", default=DEFAULT_WIN_LANG, metavar="LANG",
                      help=f"Windows ISO language "
                           f"(default: '{DEFAULT_WIN_LANG}')")
    iso_g.add_argument("--iso-arch", default=DEFAULT_WIN_ARCH, metavar="ARCH",
                      help=f"Windows ISO architecture "
                           f"(default: '{DEFAULT_WIN_ARCH}')")
    iso_g.add_argument("--no-iso-download", action="store_true",
                      help="Disable ISO auto-download. With --unattended this "
                           "makes --iso PATH mandatory (no implicit fetch).")

    # Shared-folder flags (host SMB share -> VM Z:)
    sf_g = p.add_argument_group("shared folder (host -> VM persistent storage)")
    sf_g.add_argument("--shared-folder", default=str(DEFAULT_SHARED_FOLDER),
                      metavar="PATH",
                      help=f"Host folder shared into the VM as Z:. Created if "
                           f"missing. Survives snapshot-restores. "
                           f"(default: {DEFAULT_SHARED_FOLDER})")
    sf_g.add_argument("--shared-folder-name", default=DEFAULT_SHARE_NAME,
                      metavar="NAME",
                      help=f"SMB share name on host "
                           f"(default: {DEFAULT_SHARE_NAME})")
    sf_g.add_argument("--no-shared-folder", action="store_true",
                      help="Skip shared-folder setup entirely (no SMB share, "
                           "no Z: mount inside VM).")

    p.set_defaults(func=cmd_vm_build)
