"""Shared helpers for the `sandbox` subcommand group.

Windows Sandbox is a disposable, GPU-paravirtualized Windows 11 VM built into
Windows 11 Pro/Enterprise/Education. Unlike the Hyper-V baseline VM in
`cli/vm/`, there is nothing to build, snapshot or maintain: every launch is a
pristine OS image that is discarded on close.

Measured on an RTX 4060 Ti host, with <vGPU>Default</vGPU>:
    cuInit -> 0, cuDriverGetVersion -> 13000, device0 -> NVIDIA GeForce RTX 4060 Ti
so CUDA reaches the guest through GPU-PV without installing any driver -- the
host driver store is mapped in at C:\\Windows\\System32\\HostDriverStore.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

# The feature ships WindowsSandbox.exe; the newer `wsb.exe` CLI is absent on
# some builds (verified missing on 26200), so we always drive the .exe with a
# .wsb config rather than depending on the CLI.
SANDBOX_EXE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"

# Where the guest sees our mapped folders.
GUEST_WORK_DIR = r"C:\ct"
GUEST_SRC_DIR = r"C:\src"
GUEST_LOGS_DIR = r"C:\ct-logs"

# Headroom left to the host. The guest's RAM is host physical RAM -- Sandbox
# overcommits rather than reserves -- so this is a ceiling, not a reservation.
# A lower ceiling fails better: a guest-side OOM is a clean test failure,
# whereas letting the guest take everything makes the host thrash and hangs
# the very process supervising the run.
HOST_HEADROOM_MB = 4096

TEMPLATES = Path(__file__).resolve().parent / "templates"


def _require_windows() -> None:
    if sys.platform != "win32":
        print("[sandbox] `comfy-test sandbox` is Windows-only.", file=sys.stderr)
        sys.exit(2)


def _ps(script: str, *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet. Mirrors cli/vm/_root.py::_ps."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        print("[sandbox] PowerShell not found on PATH", file=sys.stderr)
        sys.exit(2)
    cmd = [powershell, "-NoProfile", "-NonInteractive", "-Command", script]
    return subprocess.run(
        cmd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )


def sandbox_available() -> bool:
    """True if the Windows Sandbox optional feature is installed.

    Checks for the binary rather than asking DISM: Get-WindowsOptionalFeature
    spins up the servicing stack and can take minutes right after a reboot.
    """
    return SANDBOX_EXE.is_file()


def guest_memory_mb() -> int:
    """<MemoryInMB> for the guest: total physical RAM minus host headroom.

    Total, not free, so the ceiling is identical on every run -- a busy
    desktop session must not silently shrink the guest and flip a result.
    """
    r = _ps("[int]((Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize/1KB)",
            capture=True)
    total_mb = int(r.stdout.strip())
    return max(2048, total_mb - HOST_HEADROOM_MB)


def sandbox_root() -> Path:
    """Host dir holding per-run work dirs.

    NOT under %TEMP%: a <HostFolder> beneath C:\\Windows\\TEMP is silently
    rejected by Sandbox -- the guest boots, <LogonCommand> never fires,
    nothing is written and no error surfaces.
    """
    env = os.environ.get("COMFY_TEST_SANDBOX_ROOT")
    root = Path(env) if env else Path.home() / ".comfy-test-cache" / "sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def grant_sandbox_acl(path: Path) -> None:
    """Give the guest's WDAGUtilityAccount access to a mapped folder.

    Same problem cli/docker/run.py solves for ContainerUser: the guest user
    maps to the host Users SID, and without this the guest gets
    'Access to the path ... is denied' on the mapped folder.
    """
    subprocess.run(
        ["icacls", str(path), "/grant", "Users:(OI)(CI)F", "/T", "/Q"],
        check=False, capture_output=True, text=True,
    )


def render(template_name: str, subs: dict) -> str:
    """Fill a <<NAME>> template. Mirrors cli/vm/_unattend.py::_substitute."""
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(f"<<{key}>>", str(value))
    return text


def render_wsb(*, memory_mb: int, work_dir: Path, src_dir: Path,
               run_root_dir: Path) -> str:
    """Render the .wsb config. Every interpolated path is XML-escaped."""
    return render("sandbox.wsb.in", {
        "MEMORY_MB": memory_mb,
        "WORK_DIR": xml_escape(str(work_dir)),
        "SRC_DIR": xml_escape(str(src_dir)),
        "RUN_ROOT_DIR": xml_escape(str(run_root_dir)),
        "GUEST_WORK_DIR": xml_escape(GUEST_WORK_DIR),
        "GUEST_SRC_DIR": xml_escape(GUEST_SRC_DIR),
        "GUEST_LOGS_DIR": xml_escape(GUEST_LOGS_DIR),
    })


def kill_running_sandboxes() -> None:
    """Only one sandbox instance can run at a time, so clear any stragglers."""
    _ps("Get-Process -Name WindowsSandbox,WindowsSandboxClient,"
        "WindowsSandboxRemoteSession -ErrorAction SilentlyContinue "
        "| Stop-Process -Force", check=False)
