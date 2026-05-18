"""Dev-branch desktop test runner. Sibling of `_desktop_runner.py`.

Diff vs the production runner (`_desktop_runner.run_desktop`):
- Pre-writes ComfyUI-Manager's config.ini with `security_level = weak`
  so the /customnode/install/git_url HTTP endpoint is reachable.
- Honors `--branch` end-to-end: clones that branch for workflow
  enumeration, forwards it via NODE_BRANCH env so cdp_driver_dev posts
  `<repo>@<branch>` to Manager's git-clone install endpoint.
- Invokes `cdp_driver_dev.py` (not `cdp_driver.py`).
- Names artifact dirs `macos-desktop-dev` / `windows-desktop-dev` so dev
  results don't collide with the production registry-tile-install results.

The production `--desktop` path is untouched; this module imports its
helpers (kill, wipe, launch, etc.) to stay in lockstep without copying.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from comfy_test.cli._desktop_runner import (
    _APP_DIR,
    _APP_EXE,
    _DESKTOP_PKG,
    _MERGE_LOGS,
    _collect_logs,
    _ensure_desktop_app,
    _ensure_venv,
    _generate_index,
    _kill_existing,
    _launch,
    _resolve_comfy_log,
    _resolve_user_profile,
    _start_host_screencap,
    _start_monitor_server,
    _validate_host,
    _wait_for_cdp,
    _wipe_comfy_state,
)

# Sibling of cdp_driver.py; posts to /customnode/install/git_url instead
# of driving the Manager registry-tile UI.
_CDP_DRIVER_DEV = _DESKTOP_PKG / "cdp_driver_dev.py"


def _write_manager_security_config() -> None:
    """Pre-write ComfyUI-Manager's config.ini with `security_level = weak`.

    The /customnode/install/git_url endpoint is gated by
    `is_allowed_security_level('high')` (manager_server.py:109), which on
    a loopback (Desktop) listen requires security_level in {weak, normal-}.
    Default is `normal`, which 403s. Pre-writing the config before app
    launch lets Manager pick it up via read_config() (manager_core.py:1717)
    at startup.

    Manager's config path differs by ComfyUI version
    (manager_migration.py:45):
        - new (has_system_user_api): <user_dir>/__manager/config.ini
        - legacy:                    <user_dir>/default/ComfyUI-Manager/config.ini
    We write both -- whichever Manager picks up, the value is the same.

    Desktop's user_dir is <Documents>/ComfyUI/user/ on Mac and Windows.
    Called AFTER _wipe_comfy_state so the wipe doesn't take our config
    with it.
    """
    profile = _resolve_user_profile()
    user_dir = profile / "Documents" / "ComfyUI" / "user"
    paths = [
        user_dir / "__manager" / "config.ini",
        user_dir / "default" / "ComfyUI-Manager" / "config.ini",
    ]
    body = "[default]\nsecurity_level = weak\n"
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            print(f"[desktop-dev] manager config: wrote security_level=weak -> {p}",
                  flush=True)
        except Exception as e:
            print(f"[desktop-dev] manager config: failed to write {p}: {e}",
                  file=sys.stderr, flush=True)


# Map our dev-mode labels to the base-mode strings the shared helpers
# (_kill_existing, _launch, _wait_for_cdp, _ensure_desktop_app, ...)
# already understand. v1 doesn't support a GPU variant.
_DEV_TO_BASE = {
    "mac_dev":     "mac",
    "windows_dev": "windows",
}

_PLATFORM_DIR = {
    "mac_dev":     "macos-desktop-dev",
    "windows_dev": "windows-desktop-dev",
}

_DESKTOP_PLATFORM = {
    "mac_dev":     "macos_desktop_dev",
    "windows_dev": "windows_desktop_dev",
}


def run_desktop_dev(args, desktop_mode_dev: str) -> int:
    """Dev-branch desktop test. See module docstring for diff vs run_desktop."""
    base_mode = _DEV_TO_BASE.get(desktop_mode_dev)
    if base_mode is None:
        print(f"[desktop-dev] unknown mode: {desktop_mode_dev!r} "
              f"(expected one of {sorted(_DEV_TO_BASE)})", file=sys.stderr)
        return 2

    err = _validate_host(base_mode)
    if err:
        print(f"[desktop-dev] {err}", file=sys.stderr)
        return 2

    if not _CDP_DRIVER_DEV.is_file():
        print(f"[desktop-dev] cdp_driver_dev.py not found at {_CDP_DRIVER_DEV}",
              file=sys.stderr)
        return 2

    # Wipe state, then write the Manager config. Order matters: wipe
    # blows away ~/Documents/ComfyUI which is where Manager will read
    # config.ini from on first launch.
    _kill_existing(base_mode)
    _wipe_comfy_state()
    _write_manager_security_config()

    # Auto-cleanup on exit so Ctrl+C / exception / normal exit all kill
    # the ComfyUI tree (mirrors run_desktop).
    def _cleanup_comfy_processes(*_a):
        try:
            _kill_existing(base_mode)
        except Exception:
            pass
    atexit.register(_cleanup_comfy_processes)
    def _sig_cleanup(signum, _frame):
        _cleanup_comfy_processes()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGINT, _sig_cleanup)
    if hasattr(signal, "SIGTERM"):
        try: signal.signal(signal.SIGTERM, _sig_cleanup)
        except Exception: pass

    # Branch resolution. Falls back to "main" so this entry point still
    # works without --branch (though users will almost always pass one).
    node_branch = getattr(args, "branch", None) or "main"

    # Shallow-clone the target branch for workflow enumeration. The
    # install side (cdp_driver_dev) hits Manager with `<url>@<branch>`,
    # so we want the workflow list to come from the same branch.
    from comfy_test.cli._nodelink import clone_node, expand_nodelink

    url = expand_nodelink(args.nodelink).rstrip(".git")
    node_name = url.rsplit("/", 1)[-1]

    clone_root = Path(tempfile.mkdtemp(prefix="comfy-test-desktop-dev-clone-"))
    atexit.register(lambda: shutil.rmtree(clone_root, ignore_errors=True))
    workflow_names: list[str] = []
    node_sha: Optional[str] = None
    try:
        clone_node(url, node_branch, clone_root, log_prefix="[desktop-dev]")
        workflows_dir = clone_root / node_name / "workflows"
        if workflows_dir.is_dir():
            workflow_names = sorted(p.stem for p in workflows_dir.glob("*.json"))
        try:
            sha_proc = subprocess.run(
                ["git", "-C", str(clone_root / node_name), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if sha_proc.returncode == 0:
                node_sha = sha_proc.stdout.strip() or None
        except Exception:
            pass
    except Exception as e:
        print(f"[desktop-dev] clone failed (workflow enumeration will fall back "
              f"to api.github.com): {e}", file=sys.stderr)
    print(f"[desktop-dev] node: {node_name}  (URL: {url}, branch: {node_branch}, "
          f"sha: {node_sha[:12] if node_sha else 'unknown'}, "
          f"workflows: {workflow_names})")

    # Logs dir: same shape as run_desktop, with the dev platform suffix.
    short = node_name.removeprefix("ComfyUI-")
    timestamp = datetime.now().strftime("%H%M")
    run_id = f"{short}-{timestamp}"
    branch_dir = node_branch
    platform_dir = _PLATFORM_DIR[desktop_mode_dev]
    _env_logs = os.environ.get("COMFY_TEST_LOGS_DIR")
    logs_root = Path(_env_logs) if _env_logs else Path.home() / "comfy-test-logs"
    logs_dir = logs_root / run_id / branch_dir / platform_dir
    debug_dir = logs_dir / "debug"
    for d in (logs_dir, debug_dir,
              logs_dir / "logs", logs_dir / "screenshots", logs_dir / "videos"):
        d.mkdir(parents=True, exist_ok=True)
    (logs_dir / "crash_dump.log").touch()
    print(f"[desktop-dev] logs: {logs_dir}")

    monitor_port = getattr(args, "monitor_progress", None)
    if monitor_port:
        _start_monitor_server(monitor_port, logs_dir)

    venv_python = _ensure_venv()

    app_path = _ensure_desktop_app(base_mode)
    stdout_log = debug_dir / "electron_stdout.log"
    _launch(app_path, base_mode, stdout_log)
    screencap_proc = _start_host_screencap(logs_dir, base_mode)
    print(f"[desktop-dev] launched {app_path}, waiting for DevToolsActivePort...")
    try:
        cdp_port = _wait_for_cdp(base_mode, 240)
    finally:
        if screencap_proc is not None:
            try: screencap_proc.terminate()
            except Exception: pass
    if cdp_port is None:
        print(f"[desktop-dev] CDP didn't come up within 240s "
              f"(no DevToolsActivePort)", file=sys.stderr)
        return 1
    print(f"[desktop-dev] CDP up on :{cdp_port}; running cdp_driver_dev.py via cached venv")

    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        # No GPU variant in dev v1; cdp_driver_dev still reads this env.
        "COMFY_TEST_GPU": "0",
        "COMFY_TEST_LOGS_DIR": str(logs_dir),
        "COMFY_TEST_DEBUG_DIR": str(debug_dir),
        "NODE_REPO": url.rsplit("github.com/", 1)[-1],
        # cdp_driver_dev posts `<repo>@<NODE_BRANCH>` to
        # /customnode/install/git_url -- Manager checks out that ref.
        "NODE_BRANCH": node_branch,
        "NODE_NAME": node_name,
        "COMFY_TEST_WORKFLOWS": ",".join(workflow_names),
        "COMFY_TEST_NODE_SHA": node_sha or "",
        "COMFY_TEST_DESKTOP_PLATFORM": _DESKTOP_PLATFORM[desktop_mode_dev],
        "COMFY_DESKTOP_APP_EXE": str(_APP_EXE),
        "COMFY_DESKTOP_APP_PATH": str(_APP_DIR),
        "COMFY_DESKTOP_CDP_PORT": str(cdp_port),
    })

    import threading
    session_log_path = logs_dir / "session.log"
    session_log = open(session_log_path, "w", encoding="utf-8", errors="replace")

    _comfy_tail_stop = threading.Event()
    def _tail_comfy_log():
        path = _resolve_comfy_log()
        deadline = time.time() + 600
        while path is None or not path.exists():
            if _comfy_tail_stop.is_set() or time.time() > deadline:
                return
            time.sleep(2)
            path = _resolve_comfy_log()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                while not _comfy_tail_stop.is_set():
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    sys.stdout.write(f"[comfy] {line.rstrip()}\n")
        except Exception as e:
            sys.stdout.write(f"[comfy] tail failed: {e}\n")

    tail_thread = threading.Thread(target=_tail_comfy_log, daemon=True)
    tail_thread.start()

    try:
        proc = subprocess.Popen(
            [str(venv_python), str(_CDP_DRIVER_DEV)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            session_log.write(line)
            session_log.flush()
            sys.stdout.write(line)
        rc = proc.wait()
    finally:
        _comfy_tail_stop.set()
        session_log.close()

    _collect_logs(base_mode, logs_dir / "logs")
    if _MERGE_LOGS.is_file():
        try:
            subprocess.run([sys.executable, str(_MERGE_LOGS), str(logs_dir / "logs")],
                           check=False, capture_output=True)
        except Exception:
            pass
    _generate_index(logs_dir, env["NODE_REPO"], base_mode)

    print(f"[desktop-dev] DONE (rc={rc})")
    print(f"[desktop-dev] open {logs_dir / 'index.html'} to view the report")
    return rc
