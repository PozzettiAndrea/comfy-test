"""`comfy-test vm gpu` -- swap the GPU between host and VM on demand.

Default model after `vm build`: GPU lives with the VM (snapshot includes
the DDA assignment, so per-test `vm restore` doesn't need to touch the
GPU). These commands are the manual escape hatch for the human:

    comfy-test vm gpu              # show current attachment state
    comfy-test vm gpu detach       # remount on host (reclaim for workstation)
    comfy-test vm gpu attach       # dismount from host, give back to VM

DDA mutations require the VM to be Off (Hyper-V refuses otherwise);
both attach and detach refuse to run while the VM is Running/Paused.

Both subcommands are idempotent: re-running attach when the GPU is
already on the VM is a no-op, same for detach when it's already on host.
"""

from __future__ import annotations

import sys

from ._root import (
    DEFAULT_GPU_FILTER,
    DEFAULT_VM_NAME,
    _gpu_attached_to_vm,
    _gpu_in_host_limbo,
    _gpu_location_path_for_instance,
    _gpu_on_host,
    _ps,
    _require_windows,
    _vm_exists,
    _vm_state,
)


# --- shared status output -------------------------------------------------

def _print_state(vm_name: str, gpu_filter: str) -> None:
    in_vm  = _gpu_attached_to_vm(vm_name, gpu_filter) if _vm_exists(vm_name) else None
    on_host = _gpu_on_host(gpu_filter)
    in_limbo = _gpu_in_host_limbo(gpu_filter)

    print(f"[vm gpu] filter:  '{gpu_filter}'")
    print(f"[vm gpu] vm:      '{vm_name}'" + ("" if _vm_exists(vm_name) else " (does not exist)"))
    if in_vm:
        print(f"[vm gpu] state:   ATTACHED TO VM at {in_vm}")
    elif on_host:
        print(f"[vm gpu] state:   ON HOST as PnP device {on_host}")
    elif in_limbo:
        print(f"[vm gpu] state:   HOST-DETACHED, NOT ASSIGNED at {in_limbo}")
        print(f"[vm gpu]          (run `vm gpu attach` to give it to the VM, "
              f"or remount via Mount-VMHostAssignableDevice)")
    else:
        print(f"[vm gpu] state:   NOT FOUND under filter '{gpu_filter}'")


def cmd_vm_gpu(args) -> int:
    """Default action: print the current state."""
    _require_windows()
    _print_state(args.vm_name, args.gpu_filter)
    return 0


# --- attach ---------------------------------------------------------------

def cmd_vm_gpu_attach(args) -> int:
    _require_windows()
    vm_name = args.vm_name
    gpu_filter = args.gpu_filter

    if not _vm_exists(vm_name):
        print(f"[vm gpu] VM '{vm_name}' does not exist. Run `comfy-test vm build` "
              f"first.", file=sys.stderr)
        return 1

    state = _vm_state(vm_name)
    if state and state != "Off":
        print(f"[vm gpu] VM '{vm_name}' is in state '{state}'. DDA mutations "
              f"require Off. Stop it first:\n"
              f"    Stop-VM -Name '{vm_name}' -TurnOff -Force",
              file=sys.stderr)
        return 1

    already = _gpu_attached_to_vm(vm_name, gpu_filter)
    if already:
        print(f"[vm gpu] GPU already attached to VM '{vm_name}' at {already}; "
              f"nothing to do.")
        return 0

    # Did someone Dismount-VMHostAssignableDevice'd it without assigning?
    limbo = _gpu_in_host_limbo(gpu_filter)
    if limbo:
        print(f"[vm gpu] GPU is host-detached at {limbo}; assigning to VM '{vm_name}'")
        _ps(f"Add-VMAssignableDevice -VMName '{vm_name}' -LocationPath '{limbo}'")
        print(f"[vm gpu] done. GPU now attached to VM at {limbo}")
        return 0

    # Otherwise it should be on the host as a normal PnP device.
    instance_id = _gpu_on_host(gpu_filter)
    if not instance_id:
        print(f"[vm gpu] no GPU matching filter '{gpu_filter}' on host or "
              f"already-assigned. Available display devices:", file=sys.stderr)
        _ps("Get-PnpDevice -Class Display -PresentOnly | "
            "Format-Table FriendlyName,InstanceId -AutoSize")
        print(f"\n[vm gpu] tip: pass --gpu-filter <substring> to match a "
              f"different vendor (default: '{DEFAULT_GPU_FILTER}')",
              file=sys.stderr)
        return 1

    location = _gpu_location_path_for_instance(instance_id)
    if not location:
        print(f"[vm gpu] couldn't read DEVPKEY_Device_LocationPaths for "
              f"{instance_id}", file=sys.stderr)
        return 1

    print(f"[vm gpu] GPU on host: {instance_id}")
    print(f"[vm gpu] LocationPath: {location}")
    print(f"[vm gpu] *** about to dismount the GPU from the host ***")
    print(f"[vm gpu] *** the host loses display until the GPU is reattached ***")
    if not args.yes:
        ans = input("[vm gpu] type 'yes' to proceed: ").strip().lower()
        if ans != "yes":
            print("[vm gpu] aborted")
            return 1

    _ps(f"Disable-PnpDevice -InstanceId '{instance_id}' -Confirm:$false")
    _ps(f"Dismount-VMHostAssignableDevice -LocationPath '{location}' -Force")
    _ps(f"Add-VMAssignableDevice -VMName '{vm_name}' -LocationPath '{location}'")
    print(f"[vm gpu] done. GPU now attached to VM '{vm_name}' at {location}")
    return 0


# --- detach ---------------------------------------------------------------

def cmd_vm_gpu_detach(args) -> int:
    _require_windows()
    vm_name = args.vm_name
    gpu_filter = args.gpu_filter

    state = _vm_state(vm_name) if _vm_exists(vm_name) else ""
    if state and state != "Off":
        print(f"[vm gpu] VM '{vm_name}' is in state '{state}'. DDA mutations "
              f"require Off. Stop it first:\n"
              f"    Stop-VM -Name '{vm_name}' -TurnOff -Force",
              file=sys.stderr)
        return 1

    location = _gpu_attached_to_vm(vm_name, gpu_filter) if _vm_exists(vm_name) else None
    limbo = location or _gpu_in_host_limbo(gpu_filter)

    if not limbo:
        on_host = _gpu_on_host(gpu_filter)
        if on_host:
            print(f"[vm gpu] GPU already on host (PnP device {on_host}); "
                  f"nothing to do.")
            return 0
        print(f"[vm gpu] no GPU matching filter '{gpu_filter}' is attached to "
              f"VM '{vm_name}' or in host-detached limbo; nothing to do.")
        return 0

    if location:
        print(f"[vm gpu] removing DDA assignment from VM '{vm_name}' "
              f"({location})")
        # Remove-VMAssignableDevice has no -Force; -Confirm:$false suppresses
        # the (rare) interactive prompt without flagging the cmdlet.
        _ps(f"Remove-VMAssignableDevice -VMName '{vm_name}' "
            f"-LocationPath '{location}' -Confirm:$false")

    print(f"[vm gpu] mounting back on host ({limbo})")
    _ps(f"Mount-VMHostAssignableDevice -LocationPath '{limbo}'")

    # Re-enable any matching PnP device that's now visible again. Best-effort:
    # the InstanceId comes back as soon as the device is mounted; if Windows
    # hasn't enumerated it yet, the user can re-run or just let it auto-enable.
    instance_id = _gpu_on_host(gpu_filter)
    if instance_id:
        print(f"[vm gpu] enabling PnP device {instance_id}")
        _ps(f"Enable-PnpDevice -InstanceId '{instance_id}' -Confirm:$false",
            check=False)
    else:
        print(f"[vm gpu] (PnP device not yet visible; Windows will enumerate "
              f"it shortly. If it stays disabled, run "
              f"`Enable-PnpDevice -InstanceId <id> -Confirm:$false` manually.)")

    print(f"[vm gpu] done. GPU should be available on the host again.")
    return 0


# --- argparse wiring ------------------------------------------------------

def _add_common_args(p) -> None:
    p.add_argument("--vm-name", default=DEFAULT_VM_NAME,
                   help=f"VM name (default: {DEFAULT_VM_NAME})")
    p.add_argument("--gpu-filter", default=DEFAULT_GPU_FILTER,
                   help=f"GPU vendor filter; matches PCI vendor for known "
                        f"vendors (nvidia/amd/intel) or a literal substring "
                        f"otherwise (default: '{DEFAULT_GPU_FILTER}')")


def add_vm_gpu_parser(subparsers):
    p = subparsers.add_parser(
        "gpu",
        help="Show or swap the GPU between host and VM "
             "(attach/detach DDA assignment)",
    )
    p.set_defaults(func=cmd_vm_gpu)
    _add_common_args(p)
    sp = p.add_subparsers(dest="vm_gpu_command", required=False)

    pa = sp.add_parser("attach",
                       help="Dismount GPU from host, attach to VM (idempotent)")
    _add_common_args(pa)
    pa.add_argument("--yes", "-y", action="store_true",
                    help="Skip the interactive confirm before dismounting "
                         "GPU from host")
    pa.set_defaults(func=cmd_vm_gpu_attach)

    pd = sp.add_parser("detach",
                       help="Remove GPU from VM, mount back on host (idempotent)")
    _add_common_args(pd)
    pd.set_defaults(func=cmd_vm_gpu_detach)


__all__ = [
    "add_vm_gpu_parser",
    "cmd_vm_gpu",
    "cmd_vm_gpu_attach",
    "cmd_vm_gpu_detach",
]
