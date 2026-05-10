"""`comfy-test vm restore` -- revert the baseline VM to its clean snapshot.

This is the per-test-run command that gives us "fresh state" isolation.
Used by dispatch-test.yml's windows-desktop-gpu job as the first step:
    comfy-test vm restore && comfy-test vm start
"""

from __future__ import annotations

import sys

from ._root import (
    DEFAULT_SNAPSHOT_NAME,
    DEFAULT_VM_NAME,
    _ps,
    _require_windows,
    _snapshot_exists,
    _vm_exists,
)


def cmd_vm_restore(args) -> int:
    _require_windows()
    vm_name = args.vm_name
    snap = args.snapshot

    if not _vm_exists(vm_name):
        print(f"[vm] VM '{vm_name}' does not exist. Run `comfy-test vm build` first.",
              file=sys.stderr)
        return 1
    if not _snapshot_exists(vm_name, snap):
        print(f"[vm] snapshot '{snap}' not found on VM '{vm_name}'. "
              f"Run `comfy-test vm snapshot --name {snap}` after the VM is in "
              f"its baseline state.", file=sys.stderr)
        return 1

    # Stop the VM first if it's running -- Restore-VMCheckpoint requires Off.
    print(f"[vm] stopping '{vm_name}' (if running)")
    _ps(f"Stop-VM -Name '{vm_name}' -TurnOff -Force -ErrorAction SilentlyContinue",
        check=False)

    print(f"[vm] restoring '{vm_name}' to snapshot '{snap}'")
    _ps(f"Restore-VMSnapshot -VMName '{vm_name}' -Name '{snap}' -Confirm:$false")

    if args.start:
        print(f"[vm] starting '{vm_name}'")
        _ps(f"Start-VM -Name '{vm_name}'")
        if args.wait_for_runner:
            print(f"[vm] waiting for the in-VM GHA runner to come online "
                  f"(timeout: {args.wait_for_runner}s)")
            # We can't observe the runner state from the host directly without
            # GitHub API. Best-effort: poll for VM heartbeat as a proxy.
            _ps(
                f"$deadline = (Get-Date).AddSeconds({args.wait_for_runner}); "
                f"while ((Get-Date) -lt $deadline) {{ "
                f"  $hb = (Get-VM -Name '{vm_name}').Heartbeat; "
                f"  if ($hb -eq 'OkApplicationsHealthy' -or $hb -eq 'OkApplicationsUnknown') "
                f"  {{ Write-Host '  vm: heartbeat OK'; break }} "
                f"  Start-Sleep -Seconds 2 "
                f"}}"
            )
    return 0


def add_vm_restore_parser(subparsers):
    p = subparsers.add_parser(
        "restore",
        help="Revert the baseline VM to its clean snapshot (per-test-run cleanup)",
    )
    p.add_argument("--vm-name", default=DEFAULT_VM_NAME,
                   help=f"VM name (default: {DEFAULT_VM_NAME})")
    p.add_argument("--snapshot", default=DEFAULT_SNAPSHOT_NAME,
                   help=f"Snapshot name (default: {DEFAULT_SNAPSHOT_NAME})")
    p.add_argument("--start", action="store_true", default=True,
                   help="Start the VM after restoring (default: yes)")
    p.add_argument("--no-start", dest="start", action="store_false",
                   help="Don't start the VM after restoring")
    p.add_argument("--wait-for-runner", type=int, default=180, metavar="SECONDS",
                   help="After starting, wait up to N seconds for the in-VM "
                        "heartbeat (proxy for the GHA runner being online). "
                        "0 to skip. Default: 180.")
    p.set_defaults(func=cmd_vm_restore)
