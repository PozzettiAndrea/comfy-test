"""Run a desktop test inside Windows Sandbox.

Host responsibilities: stage a work dir, render the .wsb + bootstrap script,
launch WindowsSandbox.exe, poll for the guest's completion sentinel, copy the
artifact tree back, return the guest's exit code.

Everything else -- toolchain, ComfyUI Desktop install, CDP driving -- happens
in the guest. Sandbox has no process I/O, so the mapped folder is the only
channel in or out.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ._root import (
    GUEST_LOGS_DIR,
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


# Runs in SESSION 1. Dismisses the Windows Sandbox updater's modal
# ("Update failed. Continuing with Classic Windows Sandbox. Error 0x80073cf9
# ... Would you like to submit feedback?") which BLOCKS VM start until
# answered. Measured on GeometryPack-1532: the client sat at 14 MB with the
# VM unallocated until 'No' was clicked. The dialog reappears on every launch
# while the Store update keeps failing, so dismissal is part of launching.
_DISMISS_PS1 = r'''
$deadline = (Get-Date).AddSeconds(90)
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Text;
using System.Collections.Generic;
public static class W {
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr hp, EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static List<IntPtr> All(){var l=new List<IntPtr>();EnumWindows((h,p)=>{l.Add(h);return true;},IntPtr.Zero);return l;}
  public static List<IntPtr> Kids(IntPtr hp){var l=new List<IntPtr>();EnumChildWindows(hp,(h,p)=>{l.Add(h);return true;},IntPtr.Zero);return l;}
}
"@
while ((Get-Date) -lt $deadline) {
    foreach ($h in [W]::All()) {
        if (-not [W]::IsWindowVisible($h)) { continue }
        $t = New-Object Text.StringBuilder 256
        [void][W]::GetWindowText($h, $t, 256)
        if ($t.ToString() -ne 'Windows Sandbox') { continue }
        foreach ($c in [W]::Kids($h)) {
            $ct = New-Object Text.StringBuilder 256
            [void][W]::GetWindowText($c, $ct, 256)
            $cc = New-Object Text.StringBuilder 256
            [void][W]::GetClassName($c, $cc, 256)
            if ($ct.ToString().Replace('&','') -eq 'No' -and $cc.ToString() -match 'Button') {
                [void][W]::SendMessage($c, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)  # BM_CLICK
                exit 0
            }
        }
    }
    Start-Sleep -Seconds 2
}
'''

_SANDBOX_TASK = "comfy-test-sandbox-launch"


def _launch_sandbox(wsb: Path, work_dir: Path) -> None:
    """Start WindowsSandbox.exe where it can actually boot its VM.

    From session 0 (SSH/agent shells) the client starts but the VM never
    allocates -- no window, no error, vmmem stays at 0 (GeometryPack-1532 sat
    like that indefinitely). Bridge into the interactive session via
    schtasks /it, the same pattern as the Electron app and the browser-UI
    window. Also runs the updater-dialog dismisser alongside (see
    _DISMISS_PS1).
    """
    import ctypes
    sid = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(
        ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid))
    dismiss = work_dir / "dismiss-dialog.ps1"
    dismiss.write_text(_DISMISS_PS1, encoding="ascii")

    if sid.value != 0:
        # Interactive shell: plain launch works; still run the dismisser.
        subprocess.Popen([str(SANDBOX_EXE), str(wsb)])
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy",
                          "Bypass", "-File", str(dismiss)])
        return

    import os
    user = os.environ.get("USERNAME", "")
    if not user or user.upper() == "SYSTEM" or user.endswith("$"):
        for cand in Path("C:/Users").glob("*"):
            if (cand / "AppData" / "Roaming").is_dir() and cand.name.lower() not in (
                    "default", "public", "default user", "all users"):
                user = cand.name
                break
    wrapper = work_dir / "launch-sandbox.cmd"
    wrapper.write_text(
        "@echo off\r\n"
        f'start "" "{SANDBOX_EXE}" "{wsb}"\r\n'
        f'start "" /B powershell.exe -NoProfile -ExecutionPolicy Bypass '
        f'-File "{dismiss}"\r\n',
        encoding="ascii",
    )
    for a in (["schtasks", "/end", "/tn", _SANDBOX_TASK],
              ["schtasks", "/delete", "/tn", _SANDBOX_TASK, "/f"]):
        subprocess.run(a, capture_output=True, timeout=15)
    r = subprocess.run(["schtasks", "/create", "/tn", _SANDBOX_TASK, "/f",
                        "/sc", "once", "/st", "23:59", "/ru", user, "/it",
                        "/tr", str(wrapper)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not register sandbox launch task: "
                           f"{(r.stderr or r.stdout).strip()}")
    r = subprocess.run(["schtasks", "/run", "/tn", _SANDBOX_TASK],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not run sandbox launch task: "
                           f"{(r.stderr or r.stdout).strip()}")
    print(f"[sandbox] launched in interactive session as {user!r}")


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

    # Mint the FINAL artifact dir up front and map it into the guest, so the
    # guest's comfy-test writes results/session.log/frames there directly:
    # logs are live on the host during the run, and there is no end-of-run
    # _collect() (which minted a second <run-id> at collection time and
    # nested the guest's tree inside it -- D:\logs\X-2230\...\X-2208\...).
    from .._desktop_runner import resolve_logs_dir_for_sandbox
    logs_dir = resolve_logs_dir_for_sandbox(node_name, branch, desktop_mode)
    run_root = logs_dir.parent.parent   # <logs_root>/<run-id>
    logs_dir.mkdir(parents=True, exist_ok=True)
    guest_run_dir = GUEST_LOGS_DIR + "\\" + str(logs_dir.relative_to(run_root))
    print(f"[sandbox] live artifact dir: {logs_dir}")

    print(f"[sandbox] staging comfy-test source (no .git) -> {src_dir}")
    _stage_source(src_dir)

    # Clone the node ON THE HOST and map it in. git inside the guest cannot
    # resolve github.com no matter what (hosts-file pin ignored, adapter-level
    # public DNS ignored -- three runs measured) while python-level DNS in the
    # same guest works; rather than keep debugging git's resolver inside a
    # disposable VM, remove the dependency. The host's git and network are
    # reliable, and this was the original design intent anyway (keeps any
    # credential handling host-side too; a public repo clone carries none).
    node_src = work_dir / "node-src"
    # --dev: stage the LOCAL checkout instead of cloning the branch, so
    # uncommitted changes are testable -- same semantics as --dev in the
    # non-sandbox lanes. cds sets COMFY_TEST_NODE_SOURCE_DIR to the repo it
    # printed as "Using: ...". Same .git exclusion as _stage_source: dev-host
    # .git/config files embed PATs and must never enter the guest.
    local_node_src = os.environ.get("COMFY_TEST_NODE_SOURCE_DIR", "")
    if dev and local_node_src and Path(local_node_src).is_dir():
        print(f"[sandbox] --dev: staging local node tree (no .git) "
              f"{local_node_src} -> {node_src}")
        shutil.copytree(local_node_src, node_src,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    else:
        print(f"[sandbox] host-side clone {node_url} (branch {branch}) -> {node_src}")
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "-b", branch,
             node_url + ".git", str(node_src)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            print(f"[sandbox] host clone failed:\n{r.stderr.strip()}", file=sys.stderr)
            return 1

    memory_mb = guest_memory_mb()
    print(f"[sandbox] guest memory: {memory_mb} MB (total physical - 4GB)")

    # Pin GitHub hostnames in the guest's hosts file with HOST-resolved IPs.
    # The guest's NAT DNS flakes intermittently (measured: curl resolved
    # github.com at bootstrap, Comfy Desktop downloaded gigabytes, then git
    # died on "Could not resolve host: github.com" minutes later). The host's
    # resolver is reliable; its answers are good for the guest's lifetime.
    import socket
    hosts_lines = []
    # Everything the bootstrap and the desktop wizard download from. The
    # flake is per-name and can outlast curl's 25s retry window: measured
    # GeometryPack-2142, www.nuget.org rc=6 for the full window while
    # python.org and github.com resolved fine seconds earlier.
    for h in ("github.com", "api.github.com", "codeload.github.com",
              "raw.githubusercontent.com", "objects.githubusercontent.com",
              "www.nuget.org", "api.nuget.org", "globalcdn.nuget.org",
              "pypi.org", "files.pythonhosted.org",
              "download.pytorch.org", "www.python.org"):
        try:
            hosts_lines.append(f"{socket.gethostbyname(h)} {h}")
        except OSError:
            pass
    print(f"[sandbox] pinning {len(hosts_lines)} host(s) in guest DNS")

    (work_dir / "bootstrap.ps1").write_text(render("bootstrap.ps1.in", {
        "GUEST_WORK_DIR": GUEST_WORK_DIR,
        "GUEST_SRC_DIR": GUEST_SRC_DIR,
        "GUEST_LOGS_DIR": GUEST_LOGS_DIR,
        "GUEST_RUN_DIR": guest_run_dir,
        "NODE_URL": node_url,
        "BRANCH": branch,
        "CUDA": cuda,
        "DEV": "1" if dev else "0",
        "HOSTS_LINES": "\n".join(hosts_lines),
    }), encoding="utf-8")

    wsb = work_dir / "sandbox.wsb"
    wsb.write_text(render_wsb(memory_mb=memory_mb, work_dir=work_dir,
                              src_dir=src_dir, run_root_dir=run_root),
                   encoding="utf-8")

    # WDAGUtilityAccount maps to the host Users SID; without this the guest
    # gets "Access to the path is denied" on the mapped folder.
    grant_sandbox_acl(work_dir)
    grant_sandbox_acl(run_root)

    kill_running_sandboxes()
    time.sleep(2)

    print(f"[sandbox] launching {SANDBOX_EXE.name} with {wsb.name}")
    _launch_sandbox(wsb, work_dir)

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

    from .._desktop_runner import _print_workflow_summary

    # The guest wrote artifacts directly into logs_dir via the mapped run
    # dir (see the mint at the top of this function) -- nothing to collect.
    # Archive the bootstrap log next to them for the artifact bundle.
    kill_running_sandboxes()
    for cand in ("bootstrap-final.log", "bootstrap.log"):
        src = work_dir / cand
        if src.is_file():
            try:
                shutil.copy2(src, run_root / "bootstrap.log")
            except OSError:
                pass
            break

    results = logs_dir / "results.json"
    if results.is_file():
        _print_workflow_summary(results)
    else:
        print(f"[sandbox] no results.json at {logs_dir}", file=sys.stderr)

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
