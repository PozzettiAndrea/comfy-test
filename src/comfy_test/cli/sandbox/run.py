"""Run a desktop test inside Windows Sandbox.

Host responsibilities: stage a work dir, render the .wsb + bootstrap script,
launch WindowsSandbox.exe, poll for the guest's completion sentinel, copy the
artifact tree back, return the guest's exit code.

Everything else -- toolchain, ComfyUI Desktop install, CDP driving -- happens
in the guest. Sandbox has no process I/O, so the mapped folder is the only
channel in or out.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

from ._root import (
    GUEST_SRC_DIR,
    GUEST_WORK_DIR,
    SANDBOX_EXE,
    _require_windows,
    grant_sandbox_acl,
    guest_memory_mb,
    kill_running_sandboxes,
    render,
    render_wsb,
    sandbox_available,
    sandbox_root,
)

# Source items copied into the guest. An allowlist, not an exclude-list, so
# .git can never be included by accident -- on a dev host its config embeds a
# GitHub PAT, and the guest runs untrusted third-party node code.
_SRC_ALLOWLIST = ("src", "pyproject.toml", "README.md", "LICENSE")

# Bootstrap installs a toolchain and ComfyUI Desktop from scratch every run,
# then drives a full workflow suite. Measured floor is ~9 min before any
# torch install, so this is deliberately generous.
_DEFAULT_TIMEOUT_S = 5400


def _package_root() -> Path:
    """The comfy-test source tree to ship into the guest (repo root)."""
    # .../src/comfy_test/cli/sandbox/run.py -> .../src/comfy_test -> ... -> repo
    return Path(__file__).resolve().parents[4]


def _stage_source(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = _package_root()
    for name in _SRC_ALLOWLIST:
        item = root / name
        if not item.exists():
            continue
        target = dest / name
        if item.is_dir():
            shutil.copytree(
                item, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
        else:
            shutil.copy2(item, target)


def _collect(work_dir: Path, logs_dir: Path) -> None:
    """Copy the guest's artifact tree into the host's real logs_dir.

    The guest sets COMFY_TEST_LOGS_DIR=<work>\\logs, so _desktop_runner builds
    its usual <run_id>/<branch>/<platform>/ tree underneath it. Copy that
    whole shape across so downstream consumers (results.json globs, cds show,
    publish) see exactly what a host-side run produces.
    """
    guest_logs = work_dir / "logs"
    if not guest_logs.is_dir():
        print(f"[sandbox] no artifacts at {guest_logs}", file=sys.stderr)
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    for item in guest_logs.iterdir():
        target = logs_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    print(f"[sandbox] artifacts -> {logs_dir}")


def run_in_sandbox(args, desktop_mode: str) -> int:
    """Entry point from _desktop_runner.run_desktop for windows_cuda."""
    _require_windows()
    if not sandbox_available():
        print("[sandbox] Windows Sandbox is not enabled. Enable it with:\n"
              "    Enable-WindowsOptionalFeature -Online "
              "-FeatureName Containers-DisposableClientVM -All\n"
              "  (needs an elevated shell and a reboot)", file=sys.stderr)
        return 2

    from .._nodelink import expand_nodelink

    node_url = expand_nodelink(args.nodelink).rstrip(".git")
    dev = bool(getattr(args, "dev", False))
    branch = getattr(args, "branch", None) or ("dev" if dev else "main")
    cuda = "1" if desktop_mode == "windows_cuda" else "0"

    node_name = node_url.rstrip("/").rsplit("/", 1)[-1]
    run_id = f"{node_name.removeprefix('ComfyUI-')}-{time.strftime('%H%M')}"
    work_dir = sandbox_root() / run_id
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    src_dir = work_dir / "src-stage"
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"[sandbox] staging comfy-test source (no .git) -> {src_dir}")
    _stage_source(src_dir)

    memory_mb = guest_memory_mb()
    print(f"[sandbox] guest memory: {memory_mb} MB (total physical - 4GB)")

    (work_dir / "bootstrap.ps1").write_text(render("bootstrap.ps1.in", {
        "GUEST_WORK_DIR": GUEST_WORK_DIR,
        "GUEST_SRC_DIR": GUEST_SRC_DIR,
        "NODE_URL": node_url,
        "BRANCH": branch,
        "CUDA": cuda,
        "DEV": "1" if dev else "0",
    }), encoding="utf-8")

    wsb = work_dir / "sandbox.wsb"
    wsb.write_text(render_wsb(memory_mb=memory_mb, work_dir=work_dir, src_dir=src_dir),
                   encoding="utf-8")

    # WDAGUtilityAccount maps to the host Users SID; without this the guest
    # gets "Access to the path is denied" on the mapped folder.
    grant_sandbox_acl(work_dir)

    kill_running_sandboxes()
    time.sleep(2)

    print(f"[sandbox] launching {SANDBOX_EXE.name} with {wsb.name}")
    subprocess.Popen([str(SANDBOX_EXE), str(wsb)])

    done_flag = work_dir / "done.flag"
    boot_log = work_dir / "bootstrap.log"
    deadline = time.time() + _DEFAULT_TIMEOUT_S
    tailed = 0
    try:
        while time.time() < deadline:
            if done_flag.is_file():
                break
            # Mirror the guest's bootstrap log to our stdout so a CI run isn't
            # a black box for the ~9 min before anything else appears.
            if boot_log.is_file():
                try:
                    lines = boot_log.read_text(encoding="utf-8", errors="replace").splitlines()
                    for line in lines[tailed:]:
                        print(f"[guest] {line}", flush=True)
                    tailed = len(lines)
                except OSError:
                    pass
            time.sleep(5)
        else:
            print(f"[sandbox] TIMEOUT after {_DEFAULT_TIMEOUT_S}s", file=sys.stderr)
            return 1
    finally:
        if boot_log.is_file():
            try:
                lines = boot_log.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[tailed:]:
                    print(f"[guest] {line}", flush=True)
            except OSError:
                pass

    rc_file = work_dir / "rc.txt"
    try:
        rc = int(rc_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print("[sandbox] guest wrote no usable rc.txt", file=sys.stderr)
        rc = 1

    from .._desktop_runner import _print_workflow_summary, resolve_logs_dir_for_sandbox

    logs_dir = resolve_logs_dir_for_sandbox(node_name, branch, desktop_mode)
    _collect(work_dir, logs_dir)
    kill_running_sandboxes()

    results = logs_dir / "results.json"
    if results.is_file():
        _print_workflow_summary(results)

    print(f"[sandbox] DONE (rc={rc})")
    print(f"[sandbox] report: {logs_dir / 'index.html'}")
    return rc


def cmd_sandbox_status(args) -> int:
    """`comfy-test sandbox` with no subcommand: report host readiness."""
    _require_windows()
    available = sandbox_available()
    print(f"WindowsSandbox.exe : {SANDBOX_EXE} "
          f"({'present' if available else 'MISSING'})")
    if not available:
        print("\nEnable it with (elevated, then reboot):")
        print("    Enable-WindowsOptionalFeature -Online "
              "-FeatureName Containers-DisposableClientVM -All")
        return 1
    print(f"guest memory       : {guest_memory_mb()} MB (total physical - 4GB)")
    print(f"work root          : {sandbox_root()}")
    print("\nUsed automatically by `comfy-test run <repo> --desktop --cuda`.")
    return 0


def add_sandbox_status_parser(subparsers):
    p = subparsers.add_parser("status", help="Show Windows Sandbox readiness")
    p.set_defaults(func=cmd_sandbox_status)
