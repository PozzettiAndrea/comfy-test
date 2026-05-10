"""Shared constants + helpers for the `comfy-test vm` subcommand group.

The vm subpackage manages a Hyper-V baseline VM used for windows-desktop-gpu
tests. ComfyUI Desktop is an Electron + chromium GUI app that needs an
interactive desktop session and GPU passthrough -- neither of which works
in Windows containers (Session 0 isolation, --device only on process
isolation, Hyper-V isolation forbids --device, etc.).

So instead of `docker run` we Restore-VMCheckpoint a snapshot of a baseline
VM with everything pre-installed, run the test inside via the GHA self-
hosted runner agent that's registered in the VM, and revert to baseline
between runs. Same isolation contract as `docker run --rm`, ~60s overhead.

This is what `Comfy-Org/desktop` itself does for its E2E tests; just
formalized into a CLI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Defaults; overridable per-call.
DEFAULT_VM_NAME = "ComfyDesktopGPU"
DEFAULT_SNAPSHOT_NAME = "clean-baseline"
DEFAULT_VM_ROOT = Path("D:/Hyper-V/ComfyDesktopGPU")
DEFAULT_VM_CPU = 8
DEFAULT_VM_MEMORY_GB = 16
DEFAULT_VM_DISK_GB = 200
DEFAULT_VIRTUAL_SWITCH = "Default Switch"
DEFAULT_SHARED_FOLDER = Path("D:/comfyui-shared-vm")
DEFAULT_SHARE_NAME = "ComfySharedVM"
DEFAULT_GPU_FILTER = "NVIDIA"

# Fallback NVIDIA driver when the host has no working nvidia-smi (typical
# AFTER `vm gpu attach` -- the GPU is gone from the host so nvidia-smi
# returns nothing). 581.57 is a Game Ready DCH that ships CUDA 13 and
# supports RTX 20/30/40/50 cards. Bumpable; HEAD-checked at use time
# so an unreachable version fails fast instead of producing a bad URL.
DEFAULT_NVIDIA_DRIVER_VERSION = "581.57"


def _require_windows() -> None:
    if sys.platform != "win32":
        print("[vm] `comfy-test vm` is Windows-only (uses Hyper-V).",
              file=sys.stderr)
        sys.exit(2)


def _ps(script: str, *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet. Returns CompletedProcess. Streams to stdout
    by default; pass capture=True to grab the output."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        print("[vm] PowerShell not found on PATH", file=sys.stderr)
        sys.exit(2)
    cmd = [powershell, "-NoProfile", "-NonInteractive", "-Command", script]
    return subprocess.run(
        cmd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def _vm_exists(vm_name: str) -> bool:
    r = _ps(f"if (Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue) "
            f"{{ 'yes' }} else {{ 'no' }}", capture=True)
    return r.stdout.strip() == "yes"


def _snapshot_exists(vm_name: str, snapshot_name: str) -> bool:
    r = _ps(
        f"if (Get-VMSnapshot -VMName '{vm_name}' -Name '{snapshot_name}' "
        f"-ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}",
        capture=True,
    )
    return r.stdout.strip() == "yes"


def _vm_state(vm_name: str) -> str:
    r = _ps(f"(Get-VM -Name '{vm_name}').State", capture=True)
    return (r.stdout or "").strip()


def _wait_for_vm_state(
    vm_name: str,
    target: str,
    timeout_seconds: int,
    poll_seconds: int = 10,
    on_tick=None,
) -> bool:
    """Block until `Get-VM | State` equals `target` or timeout. Returns True on
    success, False on timeout. `on_tick` (optional) is called each poll with
    the current state — handy for surfacing progress."""
    import time
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _vm_state(vm_name)
        if on_tick is not None:
            on_tick(state)
        if state == target:
            return True
        time.sleep(poll_seconds)
    return False


# --- GPU / DDA helpers -----------------------------------------------------
#
# Hyper-V's DDA model is host-XOR-VM: a device is either "on the host"
# (PnP, present, has a driver) or "assigned to a VM" (Get-VMAssignableDevice).
# In between it spends a moment as "host-detached, waiting for assignment"
# (Get-VMHostAssignableDevice). These helpers let callers reason about the
# current state cheaply so we can keep operations idempotent.

def _gpu_attached_to_vm(vm_name: str, gpu_filter: str) -> Optional[str]:
    """Return LocationPath of a DDA device on `vm_name` whose InstanceID
    contains `gpu_filter` (case-insensitive substring), or None.

    NOTE: Get-VMAssignableDevice doesn't expose a friendly name -- the only
    string field is InstanceID. PCI vendor IDs are reliable enough as a
    filter ('VEN_10DE' for NVIDIA), but to keep the UX consistent with the
    rest of the CLI we just match the user-facing filter against InstanceID
    too. NVIDIA's PCI vendor string contains the literal letters but
    'NVIDIA' won't appear in InstanceID, so we accept either an InstanceID
    substring OR map well-known friendly filters to vendor IDs.
    """
    vendor = {
        "nvidia": "VEN_10DE",
        "amd":    "VEN_1002",
        "intel":  "VEN_8086",
    }.get(gpu_filter.lower(), gpu_filter)
    r = _ps(
        f"$d = Get-VMAssignableDevice -VMName '{vm_name}' "
        f"-ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.InstanceID -like '*{vendor}*' }} | "
        f"Select-Object -First 1; "
        f"if ($d) {{ $d.LocationPath }}",
        capture=True, check=False,
    )
    out = (r.stdout or "").strip()
    return out or None


def _gpu_on_host(gpu_filter: str) -> Optional[str]:
    """Return InstanceId of a Display-class PnP device whose FriendlyName
    contains `gpu_filter` (case-insensitive substring), or None. This is
    the device-as-seen-by-Windows view -- an unattached/dismounted GPU
    will NOT show up here (it's no longer a Windows-managed PnP device)."""
    r = _ps(
        "Get-PnpDevice -Class Display -PresentOnly | "
        f"Where-Object {{ $_.FriendlyName -like '*{gpu_filter}*' }} | "
        "Select-Object -First 1 -ExpandProperty InstanceId",
        capture=True, check=False,
    )
    out = (r.stdout or "").strip()
    return out or None


def _gpu_in_host_limbo(gpu_filter: str) -> Optional[str]:
    """Return LocationPath of a device that's been Dismount-VMHostAssignableDevice'd
    on the host but not yet Add-VMAssignableDevice'd to any VM. None if no
    such device exists. Filter against either InstanceID or LocationPath
    substring."""
    vendor = {
        "nvidia": "VEN_10DE",
        "amd":    "VEN_1002",
        "intel":  "VEN_8086",
    }.get(gpu_filter.lower(), gpu_filter)
    r = _ps(
        "$d = Get-VMHostAssignableDevice -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.InstanceID -like '*{vendor}*' }} | "
        "Select-Object -First 1; "
        "if ($d) { $d.LocationPath }",
        capture=True, check=False,
    )
    out = (r.stdout or "").strip()
    return out or None


def _gpu_location_path_for_instance(instance_id: str) -> Optional[str]:
    """Look up DEVPKEY_Device_LocationPaths[0] for a host PnP InstanceId."""
    r = _ps(
        f"(Get-PnpDeviceProperty -InstanceId '{instance_id}' "
        f"-KeyName DEVPKEY_Device_LocationPaths).Data[0]",
        capture=True, check=False,
    )
    out = (r.stdout or "").strip()
    return out or None


# --- SMB share / network helpers ------------------------------------------

def _smb_share_exists(share_name: str) -> bool:
    r = _ps(
        f"if (Get-SmbShare -Name '{share_name}' -ErrorAction SilentlyContinue) "
        f"{{ 'yes' }} else {{ 'no' }}",
        capture=True, check=False,
    )
    return (r.stdout or "").strip() == "yes"


def _resolve_nvidia_driver_url(
    explicit_url: Optional[str],
    explicit_version: Optional[str] = None,
    fallback_version: str = DEFAULT_NVIDIA_DRIVER_VERSION,
) -> str:
    """Resolve a downloadable NVIDIA driver .exe URL.

    Priority:
      1. explicit_url (just return it -- caller's responsibility)
      2. explicit_version -- build candidate URLs, HEAD-check, return first OK
      3. nvidia-smi version on host (won't work after vm gpu attach)
      4. fallback_version

    Reuses the candidate-URL builder + HEAD checker from cli/docker/build.py
    so the URL templates stay in one place. Raises RuntimeError if no
    candidate URL responds 2xx/3xx.
    """
    if explicit_url:
        return explicit_url

    # Late import to avoid CLI load-time coupling between vm/ and docker/.
    from ..docker.build import (
        _http_head_ok,
        _nvidia_driver_urls,
        _query_host_driver_windows,
    )

    version = explicit_version
    if not version:
        version = _query_host_driver_windows()
        if version:
            print(f"[vm] detected host NVIDIA driver: {version}")
        else:
            version = fallback_version
            print(f"[vm] no host NVIDIA driver detected (GPU may be DDA'd "
                  f"to VM); using fallback driver version {fallback_version}")

    for url in _nvidia_driver_urls(version):
        if _http_head_ok(url):
            print(f"[vm] resolved NVIDIA driver URL: {url}")
            return url

    raise RuntimeError(
        f"no NVIDIA driver URL responded for version '{version}'. "
        f"Pass --nvidia-driver-url explicitly or try a different "
        f"--nvidia-driver-version."
    )


def _default_switch_host_ip() -> Optional[str]:
    """The Hyper-V Default Switch creates a host-only NAT network; the host's
    address on it is what VMs reach the host as. Returns None if Hyper-V's
    Default Switch is not configured."""
    r = _ps(
        "$ip = Get-NetIPAddress -InterfaceAlias 'vEthernet (Default Switch)' "
        "-AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty IPAddress; "
        "if ($ip) { $ip }",
        capture=True, check=False,
    )
    out = (r.stdout or "").strip()
    return out or None
