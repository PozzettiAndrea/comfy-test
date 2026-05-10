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
