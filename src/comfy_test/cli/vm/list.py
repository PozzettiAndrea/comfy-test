"""`comfy-test vm list` -- show known VMs, snapshots, and DDA assignments.

Default action when `comfy-test vm` is run with no subcommand. Useful to
sanity-check that the host has Hyper-V on, the baseline VM exists, and
the GPU is currently attached to it (vs the host).
"""

from __future__ import annotations

from ._root import _require_windows, _ps, DEFAULT_VM_NAME


def cmd_vm_list(args) -> int:
    _require_windows()

    vm_name = getattr(args, "vm_name", DEFAULT_VM_NAME)

    # Probe Hyper-V state. If disabled / not present, none of the
    # Hyper-V cmdlets (Get-VM, Get-VMSnapshot, Get-VMAssignableDevice,
    # Get-VMHostAssignableDevice) will resolve, so short-circuit with a
    # friendly message.
    print(f"[vm] === Hyper-V state ===")
    r = _ps(
        "$f = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All "
        "-ErrorAction SilentlyContinue; "
        "if ($f) { $f.State } else { 'NotPresent' }",
        capture=True,
    )
    state = (r.stdout or "").strip()
    print(f"Hyper-V: {state or 'unknown'}")
    if state != "Enabled":
        print("\n[vm] Hyper-V is not enabled on this host.")
        print("[vm] Run `comfy-test vm build` (as Administrator) to enable it "
              "and create the baseline VM. A reboot is required after enabling.")
        return 0

    print(f"\n[vm] === VMs ===")
    _ps("Get-VM | Format-Table Name,State,CPUUsage,MemoryAssigned,Uptime "
        "-AutoSize")

    print(f"\n[vm] === Snapshots for '{vm_name}' (if exists) ===")
    _ps(
        f"if (Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue) {{ "
        f"  Get-VMSnapshot -VMName '{vm_name}' | "
        f"  Format-Table Name,SnapshotType,CreationTime -AutoSize "
        f"}} else {{ Write-Host '  (no VM named {vm_name})' }}"
    )

    print(f"\n[vm] === DDA-attached devices on '{vm_name}' (if exists) ===")
    _ps(
        f"if (Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue) {{ "
        f"  $devs = Get-VMAssignableDevice -VMName '{vm_name}'; "
        f"  if ($devs) {{ $devs | Format-Table InstanceID,LocationPath -AutoSize }} "
        f"  else {{ Write-Host '  (no DDA devices attached)' }} "
        f"}} else {{ Write-Host '  (no VM named {vm_name})' }}"
    )

    print(f"\n[vm] === Host-detached PnP devices waiting for DDA ===")
    _ps("Get-VMHostAssignableDevice | Format-Table InstanceID,LocationPath "
        "-AutoSize")

    return 0


def add_vm_list_parser(subparsers):
    p = subparsers.add_parser(
        "list",
        help="Show Hyper-V state, VMs, snapshots, and DDA-attached devices",
    )
    p.add_argument("--vm-name", default=DEFAULT_VM_NAME,
                   help=f"VM name to inspect (default: {DEFAULT_VM_NAME})")
    p.set_defaults(func=cmd_vm_list)
