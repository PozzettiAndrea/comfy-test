r"""`comfy-test vm share` -- expose a host folder to the VM as a network drive.

Hyper-V checkpoints include the entire VM disk (VHDX), so anything written
inside the VM gets reverted by `vm restore`. For files that should survive
restores -- model weights, output dumps, dataset caches -- we mount a host
folder into the VM over SMB. The share contents live on the host's
filesystem, completely outside the VHDX, so checkpoints don't touch them.

Subcommands:

    comfy-test vm share              # list current shares
    comfy-test vm share create       # mkdir + New-SmbShare on host
    comfy-test vm share remove       # Remove-SmbShare on host

The companion side -- mounting `\\<host-ip>\<share>` as `Z:` inside the VM
-- happens during `vm build --unattended` (post-install.ps1 runs
`New-PSDrive -Persist`). For an existing VM, the user runs the same
`net use` / `New-PSDrive` line manually inside the VM (printed by
`vm share create`).

Default folder: D:/comfyui-shared-vm. Default share name: ComfySharedVM.
Access is granted to the local 'ci-runner' account (matches the VM's
autologon user).
"""

from __future__ import annotations

import sys
from pathlib import Path

from ._root import (
    DEFAULT_SHARE_NAME,
    DEFAULT_SHARED_FOLDER,
    _default_switch_host_ip,
    _ps,
    _require_windows,
    _smb_share_exists,
)


def _print_unc(share_name: str) -> None:
    """Print the UNC the VM should mount, plus a copy-paste line."""
    ip = _default_switch_host_ip()
    if not ip:
        print(f"[vm share] (couldn't read 'vEthernet (Default Switch)' IP; "
              f"check Hyper-V is configured)", file=sys.stderr)
        return
    unc = f"\\\\{ip}\\{share_name}"
    print(f"[vm share] UNC for VM:      {unc}")
    print(f"[vm share] in-VM mount:     New-PSDrive -Name Z -PSProvider FileSystem "
          f"-Root '{unc}' -Persist -Credential (Get-Credential)")


def cmd_vm_share_list(args) -> int:
    _require_windows()
    print("[vm share] === host SMB shares ===")
    _ps("Get-SmbShare | Format-Table Name,Path,ScopeName,Description -AutoSize",
        check=False)
    return 0


def cmd_vm_share_create(args) -> int:
    _require_windows()
    folder = Path(args.path).resolve()
    name = args.name
    user = args.user

    print(f"[vm share] ensuring folder exists: {folder}")
    folder.mkdir(parents=True, exist_ok=True)

    if _smb_share_exists(name):
        print(f"[vm share] share '{name}' already exists; updating path "
              f"+ permissions")
        # Remove + recreate is simpler than reasoning about per-attribute
        # updates and only touches the share metadata, not the folder.
        _ps(f"Remove-SmbShare -Name '{name}' -Force")

    print(f"[vm share] creating SMB share '{name}' -> {folder} "
          f"(full access for '{user}')")
    _ps(
        f"New-SmbShare -Name '{name}' -Path '{folder}' "
        f"-FullAccess '{user}' -Description 'comfy-test VM persistent storage'"
    )

    print(f"[vm share] done.")
    _print_unc(name)
    return 0


def cmd_vm_share_remove(args) -> int:
    _require_windows()
    name = args.name
    if not _smb_share_exists(name):
        print(f"[vm share] share '{name}' does not exist; nothing to do.")
        return 0
    print(f"[vm share] removing SMB share '{name}' (folder contents kept)")
    _ps(f"Remove-SmbShare -Name '{name}' -Force")
    print(f"[vm share] done.")
    return 0


def cmd_vm_share(args) -> int:
    """Default action: list shares."""
    return cmd_vm_share_list(args)


# --- argparse wiring ------------------------------------------------------

def add_vm_share_parser(subparsers):
    p = subparsers.add_parser(
        "share",
        help="Expose a host folder to the VM as an SMB share "
             "(persistent storage that survives snapshot-restores)",
    )
    p.set_defaults(func=cmd_vm_share)
    sp = p.add_subparsers(dest="vm_share_command", required=False)

    pl = sp.add_parser("list", help="List current SMB shares on the host")
    pl.set_defaults(func=cmd_vm_share_list)

    pc = sp.add_parser("create",
                       help="Create (or overwrite) the SMB share for the VM")
    pc.add_argument("--path", default=str(DEFAULT_SHARED_FOLDER), metavar="PATH",
                    help=f"Host folder to share; created if missing "
                         f"(default: {DEFAULT_SHARED_FOLDER})")
    pc.add_argument("--name", default=DEFAULT_SHARE_NAME, metavar="NAME",
                    help=f"SMB share name (default: {DEFAULT_SHARE_NAME})")
    pc.add_argument("--user", default="ci-runner", metavar="USER",
                    help="Local account granted FullAccess on the share "
                         "(default: 'ci-runner', matches VM autologon user)")
    pc.set_defaults(func=cmd_vm_share_create)

    pr = sp.add_parser("remove", help="Remove the SMB share (folder kept)")
    pr.add_argument("--name", default=DEFAULT_SHARE_NAME, metavar="NAME",
                    help=f"SMB share name to remove (default: {DEFAULT_SHARE_NAME})")
    pr.set_defaults(func=cmd_vm_share_remove)


__all__ = [
    "add_vm_share_parser",
    "cmd_vm_share",
    "cmd_vm_share_create",
    "cmd_vm_share_list",
    "cmd_vm_share_remove",
]
