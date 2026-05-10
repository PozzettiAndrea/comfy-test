"""`comfy-test vm snapshot` -- take a checkpoint of the baseline VM.

Used after the in-VM setup is complete (NVIDIA driver, ComfyUI Desktop,
comfy-test, autologon, GHA runner all installed) to capture the clean
state that subsequent `comfy-test vm restore` calls will revert to.

Default: stops the VM first (snapshots while running create a saved-state
snapshot which is heavier and slower to restore than a stopped snapshot).
"""

from __future__ import annotations

import sys

from ._root import (
    DEFAULT_SNAPSHOT_NAME,
    DEFAULT_VM_NAME,
    _ps,
    _require_windows,
    _vm_exists,
)


def cmd_vm_snapshot(args) -> int:
    _require_windows()
    vm_name = args.vm_name
    snap = args.name

    if not _vm_exists(vm_name):
        print(f"[vm] VM '{vm_name}' does not exist. Run `comfy-test vm build` first.",
              file=sys.stderr)
        return 1

    if args.shutdown_first:
        print(f"[vm] stopping '{vm_name}' for clean snapshot")
        _ps(f"Stop-VM -Name '{vm_name}' -Force -ErrorAction SilentlyContinue",
            check=False)

    print(f"[vm] checkpointing '{vm_name}' as '{snap}'")
    _ps(f"Checkpoint-VM -Name '{vm_name}' -SnapshotName '{snap}'")
    print(f"[vm] done. Use `comfy-test vm restore --snapshot {snap}` to revert.")
    return 0


def add_vm_snapshot_parser(subparsers):
    p = subparsers.add_parser(
        "snapshot",
        help="Take a Hyper-V checkpoint of the baseline VM",
    )
    p.add_argument("--vm-name", default=DEFAULT_VM_NAME,
                   help=f"VM name (default: {DEFAULT_VM_NAME})")
    p.add_argument("--name", default=DEFAULT_SNAPSHOT_NAME,
                   help=f"Snapshot name (default: {DEFAULT_SNAPSHOT_NAME})")
    p.add_argument("--keep-running", dest="shutdown_first",
                   action="store_false", default=True,
                   help="Snapshot while the VM is running (heavier; default is "
                        "to stop first for a smaller / faster-to-restore snapshot)")
    p.set_defaults(func=cmd_vm_snapshot)
