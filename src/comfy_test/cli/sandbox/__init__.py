"""`comfy-test sandbox` subcommand group.

Runs windows-desktop-cuda tests inside Windows Sandbox instead of the Hyper-V
baseline VM in `cli/vm/`.

The VM's stated rationale was that ComfyUI Desktop needs an interactive
desktop session AND GPU passthrough, neither available in Windows containers.
The first half is true -- containers are Session 0 and Electron cannot render
there. The second half is not: `cli/docker/run.py` already passes CUDA into a
process-isolated container via --device class/5B45201D-..., and that was
verified working on this hardware.

Windows Sandbox satisfies both at once, plus the requirement the VM docstring
never stated but which actually justifies isolation: containing the untrusted
third-party node code this harness deliberately runs with Manager's
security_level=weak. It needs no image build, no snapshot lifecycle, no DDA
GPU dismount, and no driver-version matching -- GPU-PV maps the host driver
store straight into a pristine guest.

Verified on an RTX 4060 Ti / Windows 11 Pro 26200 host:
    cuInit -> 0, cuDriverGetVersion -> 13000, RTX 4060 Ti visible
    ComfyUI Desktop installs (rc=0) and exposes DevToolsActivePort in ~20s

Subcommands:
    status -- Show host readiness (feature installed, computed guest memory).
              Default action when run with no subcommand.
"""

from .run import add_sandbox_status_parser, cmd_sandbox_status, run_in_sandbox


def add_sandbox_parser(subparsers):
    """Register the `sandbox` subcommand group."""
    p = subparsers.add_parser(
        "sandbox",
        help="Windows Sandbox runner for windows-desktop-cuda tests",
    )
    # Bare `comfy-test sandbox` (no subcommand) defaults to `status`.
    p.set_defaults(func=cmd_sandbox_status)
    sp = p.add_subparsers(dest="sandbox_command", required=False)
    add_sandbox_status_parser(sp)


__all__ = [
    "add_sandbox_parser",
    "cmd_sandbox_status",
    "run_in_sandbox",
]
