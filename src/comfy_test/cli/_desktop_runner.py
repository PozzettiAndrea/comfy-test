"""Local desktop test runner — same flow as
`.github/workflows/_test-{macos,windows}-desktop.yml` but executed on the
host rather than a GHA runner. Used by `comfy-test dockertest --desktop_*`
to iterate on cdp_driver.py without round-tripping through CI.

Mirrors the YML's responsibilities:
- Resolve / download ComfyUI Desktop install
- Clone the target node repo (delegates to dockertest._clone_node)
- Launch the Desktop app with --remote-debugging-port=9222
- Run scripts/cdp_driver.py against the live app
- Collect logs from Desktop's standard log paths
- Touch crash_dump.log + render per-platform index.html
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


def _download(url: str, dest: Path) -> None:
    """Download via curl. urllib's default User-Agent gets 403'd by the
    download.comfy.org → dl.todesktop.com CDN; curl with -L --retry 3
    matches what the YMLs do and works."""
    print(f"[desktop] downloading {url} -> {dest}")
    subprocess.run(
        ["curl", "-L", "--retry", "3", "--fail", "-A", "Mozilla/5.0",
         "-o", str(dest), url],
        check=True,
    )

# `desktop_mode` -> dict of platform-specific settings.
# Repo paths are absolute so this works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CDP_DRIVER = _REPO_ROOT / ".github" / "workflows" / "scripts" / "cdp_driver.py"
_MERGE_LOGS = _REPO_ROOT / ".github" / "workflows" / "scripts" / "merge_logs.py"

# All host-side state lives under here so a `dockertest --desktop_*` run
# leaves nothing behind on the host outside this dir (other than the
# ComfyUI Desktop's own runtime data dir at ~/Documents/ComfyUI which is
# managed by the app itself, not by us).
_CACHE_DIR = Path.home() / ".comfy-test-cache" / "desktop"
_APP_DIR = _CACHE_DIR / "ComfyUI.app"          # mac
_APP_EXE = _CACHE_DIR / "ComfyUI" / "ComfyUI.exe"  # windows portable-ish layout
_VENV_DIR = _CACHE_DIR / "venv"

_DESKTOP_DOWNLOAD_URLS = {
    "mac":         "https://download.comfy.org/mac/dmg/arm64",
    "windows":     "https://download.comfy.org/windows/nsis/x64",
    "windows_gpu": "https://download.comfy.org/windows/nsis/x64",
}


def _host_kind() -> str:
    """Return 'mac' | 'windows' | 'linux' for the current host."""
    s = sys.platform
    if s == "darwin":
        return "mac"
    if s.startswith("win"):
        return "windows"
    return "linux"


def _validate_host(desktop_mode: str) -> Optional[str]:
    host = _host_kind()
    if desktop_mode == "mac" and host != "mac":
        return f"--desktop_mac requires a macOS host, got {host}"
    if desktop_mode in ("windows", "windows_gpu") and host != "windows":
        return f"--{desktop_mode.replace('_', '-')} requires a Windows host, got {host}"
    # On macOS, GUI apps can only launch in the user's `gui/<uid>` launchd
    # session. Running this command from an SSH session puts us in a
    # `Background` session where ComfyUI Desktop silently zombies (32-KB
    # RSS, no children, no ports, no stdout — nothing). Detect and bail
    # with a clear explanation rather than the 60s CDP-poll timeout.
    if desktop_mode == "mac" and os.environ.get("SSH_CONNECTION"):
        return (
            "--desktop_mac doesn't work from an SSH session — macOS GUI apps\n"
            "  can't launch in the Background launchd session that SSH gives you.\n"
            "  Run from Terminal.app on the physical Mac console, OR re-run with\n"
            "  `sudo launchctl asuser <uid> python -m comfy_test dockertest ... --desktop_mac`.\n"
            f"  Detected SSH_CONNECTION={os.environ['SSH_CONNECTION']!r}."
        )
    return None


def _ensure_desktop_app(desktop_mode: str) -> Path:
    """Cache ComfyUI Desktop into our private dir and return the launchable
    path. Never touches /Applications or %LOCALAPPDATA%\\Programs — the
    whole point of `dockertest` is isolation, so the host stays clean.
    A subsequent run reuses the cached copy unless --refresh-app is passed.

    Returns the .app dir on macOS, the .exe path on Windows.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if desktop_mode == "mac":
        if _APP_DIR.exists():
            print(f"[desktop] reusing cached app at {_APP_DIR}")
            return _APP_DIR
        dmg = _CACHE_DIR / "comfyui-desktop.dmg"
        _download(_DESKTOP_DOWNLOAD_URLS["mac"], dmg)
        # Mount, copy app, detach. The DMG mount path includes a versioned
        # suffix (e.g. "ComfyUI 0.8.36-arm64") that varies per release; glob to find it.
        subprocess.run(["hdiutil", "attach", "-nobrowse", str(dmg)], check=True)
        try:
            mounts = list(Path("/Volumes").glob("ComfyUI*"))
            if not mounts:
                raise RuntimeError("ComfyUI mount not found under /Volumes after hdiutil attach")
            src = mounts[0] / "ComfyUI.app"
            print(f"[desktop] copying {src} -> {_APP_DIR}")
            # cp -R preserves the framework symlinks
            # (Versions/Current -> A, top-level binary -> Versions/Current/Foo).
            # shutil.copytree defaults to symlinks=False which dereferences
            # them, materializing every framework Version as a full copy
            # and producing a bundle Gatekeeper rejects with
            # "bundle format is ambiguous (could be app or framework)".
            subprocess.run(["cp", "-R", str(src), str(_APP_DIR)], check=True)
        finally:
            for m in Path("/Volumes").glob("ComfyUI*"):
                subprocess.run(["hdiutil", "detach", str(m)], capture_output=True)
        dmg.unlink(missing_ok=True)
        # Strip the quarantine xattr that Gatekeeper sets on downloaded
        # apps; otherwise first launch pops a "open anyway?" dialog the
        # CDP driver can't dismiss.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(_APP_DIR)],
                       capture_output=True)
        return _APP_DIR

    # windows / windows_gpu
    if _APP_EXE.exists():
        print(f"[desktop] reusing cached app at {_APP_EXE}")
        return _APP_EXE
    setup = _CACHE_DIR / "ComfyUI-Setup.exe"
    _download(_DESKTOP_DOWNLOAD_URLS["windows"], setup)
    # NSIS supports /D for install dir. Use our cache root so the install
    # doesn't pollute %LOCALAPPDATA%\Programs\ComfyUI on the host.
    install_dir = _CACHE_DIR / "ComfyUI"
    subprocess.run([str(setup), "/S", f"/D={install_dir}"], check=True)
    for _ in range(180):
        if _APP_EXE.exists():
            return _APP_EXE
        time.sleep(1)
    raise RuntimeError(f"ComfyUI.exe not present at {_APP_EXE} after silent install")


def _ensure_venv() -> Path:
    """Create a private venv with playwright + imageio-ffmpeg + tomli +
    chromium browser. Reuses on subsequent runs.

    Returns the path to the venv's python executable.
    """
    if sys.platform == "win32":
        venv_python = _VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = _VENV_DIR / "bin" / "python"

    if venv_python.exists():
        # Verify deps are still importable; fast path.
        ok = subprocess.run(
            [str(venv_python), "-c",
             "import playwright, imageio_ffmpeg, tomli; print('ok')"],
            capture_output=True, text=True,
        )
        if ok.returncode == 0:
            print(f"[desktop] reusing venv at {_VENV_DIR}")
            return venv_python

    print(f"[desktop] creating venv at {_VENV_DIR}")
    import venv as _venv  # stdlib
    _venv.EnvBuilder(with_pip=True, clear=True).create(str(_VENV_DIR))
    subprocess.run([str(venv_python), "-m", "pip", "install", "--quiet",
                    "playwright", "imageio-ffmpeg", "tomli"], check=True)
    print("[desktop] installing chromium for playwright (~150 MB)...")
    subprocess.run([str(venv_python), "-m", "playwright", "install", "chromium"],
                   check=True)
    return venv_python


def _kill_existing(desktop_mode: str) -> None:
    """Kill any running ComfyUI process so our --remote-debugging-port flag takes effect.
    (On a re-launched-already process, the flag is ignored.)"""
    if desktop_mode == "mac":
        subprocess.run(["pkill", "-f", "ComfyUI"], capture_output=True)
    else:
        subprocess.run(["taskkill", "/F", "/IM", "ComfyUI.exe"], capture_output=True)
    time.sleep(2)


def _launch(app_path: Path, desktop_mode: str, stdout_log: Path) -> None:
    """Launch the Desktop app with CDP enabled. App stdout goes to stdout_log."""
    out_fh = open(stdout_log, "wb")
    if desktop_mode == "mac":
        # `open --args` forwards flags to the Electron main process argv.
        subprocess.Popen(
            ["open", str(app_path), "--args", "--remote-debugging-port=9222"],
            stdout=out_fh, stderr=out_fh,
        )
    else:
        subprocess.Popen(
            [str(app_path), "--remote-debugging-port=9222"],
            stdout=out_fh, stderr=out_fh,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )


def _wait_for_cdp(timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def _collect_logs(desktop_mode: str, dest: Path) -> None:
    """Copy ComfyUI Desktop's runtime logs into dest. Same source paths as the YMLs."""
    dest.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    if desktop_mode == "mac":
        sources = [
            Path.home() / "Documents" / "ComfyUI" / "user",
            Path.home() / "Library" / "Logs" / "ComfyUI",
            Path.home() / "Library" / "Application Support" / "ComfyUI" / "logs",
        ]
    else:
        appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        sources = [appdata / "ComfyUI" / "logs"]
    for src in sources:
        if not src.is_dir():
            continue
        for p in src.rglob("*.log"):
            try:
                shutil.copy2(p, dest / p.name)
            except Exception:
                pass


def _generate_index(logs_dir: Path, node_repo: str, desktop_mode: str) -> None:
    """Render per-platform index.html into logs_dir using the framework's
    own report generator. Skips with a warning on import error so a missing
    optional dep doesn't fail the whole run."""
    platform_id = {"mac": "macos-desktop",
                   "windows": "windows-desktop",
                   "windows_gpu": "windows-desktop-gpu"}[desktop_mode]
    try:
        from comfy_test.reporting.html_report import generate_html_report
        generate_html_report(logs_dir, repo_name=node_repo, current_platform=platform_id)
        print(f"[desktop] wrote {logs_dir / 'index.html'}")
    except Exception as e:
        print(f"[desktop] index.html generation skipped: {e}", file=sys.stderr)


def run_desktop(args, desktop_mode: str) -> int:
    """Local-host equivalent of the desktop YMLs. Returns process rc."""
    err = _validate_host(desktop_mode)
    if err:
        print(f"[desktop] {err}", file=sys.stderr)
        return 2

    if not _CDP_DRIVER.is_file():
        print(f"[desktop] cdp_driver.py not found at {_CDP_DRIVER}", file=sys.stderr)
        return 2

    # Clone the target node — same helper dockertest uses.
    from comfy_test.cli.dockertest import _clone_node, _expand_nodelink  # local import: avoids cycles

    work_root = Path.home() / ".comfy-test-cache" / "desktop-runs"
    work_root.mkdir(parents=True, exist_ok=True)
    clone_root = Path(work_root) / f"clone-{int(time.time())}"
    try:
        node_name = _clone_node(args.nodelink, args.branch, clone_root)
    except Exception as e:
        print(f"[desktop] clone failed: {e}", file=sys.stderr)
        return 1
    print(f"[desktop] node: {node_name}  (cloned to {clone_root / node_name})")

    # Logs dir mirrors run.py's <short_name>-<HHMM> shape.
    short = node_name.removeprefix("ComfyUI-")
    timestamp = datetime.now().strftime("%H%M")
    run_id = f"{short}-{timestamp}"
    logs_root = Path.home() / "comfy-test-logs"
    logs_dir = logs_root / run_id
    debug_dir = logs_dir / "debug"
    for d in (logs_dir, debug_dir,
              logs_dir / "logs", logs_dir / "screenshots", logs_dir / "videos"):
        d.mkdir(parents=True, exist_ok=True)
    (logs_dir / "crash_dump.log").touch()
    print(f"[desktop] logs: {logs_dir}")

    # Bootstrap an isolated venv with playwright + chromium + ffmpeg so the
    # host's system Python (or homebrew python) doesn't get touched.
    venv_python = _ensure_venv()

    # Bootstrap Desktop install + launch.
    app_path = _ensure_desktop_app(desktop_mode)
    _kill_existing(desktop_mode)
    stdout_log = debug_dir / "electron_stdout.log"
    _launch(app_path, desktop_mode, stdout_log)
    print(f"[desktop] launched {app_path}, polling CDP on :9222...")
    if not _wait_for_cdp(60):
        print("[desktop] CDP didn't come up within 60s", file=sys.stderr)
        return 1
    print("[desktop] CDP up; running cdp_driver.py via cached venv")

    # Drive the app via cdp_driver. Env vars match what the YMLs set.
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "COMFY_TEST_GPU": "1" if desktop_mode == "windows_gpu" else "0",
        "COMFY_TEST_LOGS_DIR": str(logs_dir),
        "COMFY_TEST_DEBUG_DIR": str(debug_dir),
        "NODE_REPO": _expand_nodelink(args.nodelink).rstrip(".git").rsplit("github.com/", 1)[-1],
        "NODE_BRANCH": args.branch or "main",
        "NODE_NAME": node_name,
    })
    session_log = open(logs_dir / "session.log", "w", encoding="utf-8")
    try:
        rc = subprocess.call(
            [str(venv_python), str(_CDP_DRIVER)],
            env=env, stdout=session_log, stderr=subprocess.STDOUT,
        )
    finally:
        session_log.close()

    # Post-run: collect Desktop logs, merge them, render index.html.
    _collect_logs(desktop_mode, logs_dir / "logs")
    if _MERGE_LOGS.is_file():
        try:
            subprocess.run([sys.executable, str(_MERGE_LOGS), str(logs_dir / "logs")],
                           check=False, capture_output=True)
        except Exception:
            pass
    _generate_index(logs_dir, env["NODE_REPO"], desktop_mode)

    # Best-effort: leave the Desktop app open so the user can poke around.
    print(f"[desktop] DONE (rc={rc})")
    print(f"[desktop] open {logs_dir / 'index.html'} to view the report")
    return rc
