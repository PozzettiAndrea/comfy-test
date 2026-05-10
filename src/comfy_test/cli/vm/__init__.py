"""`comfy-test vm` subcommand group.

Manages a Hyper-V baseline VM used for windows-desktop-gpu CI tests.
ComfyUI Desktop is an Electron + chromium GUI app that needs an interactive
desktop session and GPU passthrough -- neither of which works in Windows
containers (Session 0 isolation, --device only on process isolation,
Hyper-V isolation forbids --device, etc.).

So instead of `docker run --rm` we Restore-VMCheckpoint a baseline snapshot
between every test run. Same isolation contract, ~60s overhead.

Subcommands:
    list      -- Show Hyper-V state, VMs, snapshots, DDA assignments.
                 Default action when `comfy-test vm` is run with no subcommand.
    build     -- One-time host setup: enable Hyper-V, create baseline VM,
                 DDA-attach the GPU. Run as Administrator.
    snapshot  -- Take a Hyper-V checkpoint (after the in-VM setup is complete).
    restore   -- Revert the baseline VM to its clean snapshot
                 (per-test-run cleanup; called from dispatch-test.yml).
"""

from .build import add_vm_build_parser, cmd_vm_build
from .list import add_vm_list_parser, cmd_vm_list
from .restore import add_vm_restore_parser, cmd_vm_restore
from .snapshot import add_vm_snapshot_parser, cmd_vm_snapshot


def add_vm_parser(subparsers):
    """Register the `vm` subcommand group."""
    p = subparsers.add_parser(
        "vm",
        help="Hyper-V baseline VM lifecycle for windows-desktop-gpu tests",
    )
    # Bare `comfy-test vm` (no subcommand) defaults to `list`.
    p.set_defaults(func=cmd_vm_list)
    sp = p.add_subparsers(dest="vm_command", required=False)
    add_vm_list_parser(sp)
    add_vm_build_parser(sp)
    add_vm_snapshot_parser(sp)
    add_vm_restore_parser(sp)


__all__ = [
    "add_vm_parser",
    "cmd_vm_build",
    "cmd_vm_list",
    "cmd_vm_restore",
    "cmd_vm_snapshot",
]
