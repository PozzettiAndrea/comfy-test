"""Local desktop test runner -- same flow as
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

import atexit
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


def _download(url: str, dest: Path) -> None:
    """Download via curl. urllib's default User-Agent gets 403'd by the
    download.comfy.org -> dl.todesktop.com CDN; curl with -L --retry 3
    matches what the YMLs do and works."""
    print(f"[desktop] downloading {url} -> {dest}")
    subprocess.run(
        ["curl", "-L", "--retry", "3", "--fail", "-A", "Mozilla/5.0",
         "-o", str(dest), url],
        check=True,
    )

# `desktop_mode` -> dict of platform-specific settings.
# Scripts ship inside the package so they're available after pip install.
_DESKTOP_PKG = Path(__file__).resolve().parent.parent / "desktop"
_CDP_DRIVER = _DESKTOP_PKG / "cdp_driver.py"
_MERGE_LOGS = _DESKTOP_PKG / "merge_logs.py"

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
    # SSH-spawned shells (incl. loopback ones, like the one limactl/colima
    # holds open against the host's own sshd) put the process in a Background
    # launchd session, where `open <app>` silently zombies. We auto-bridge at
    # the launch site via `sudo launchctl asuser <uid>`, so don't bail here.
    return None


def _ensure_desktop_app(desktop_mode: str) -> Path:
    """Cache ComfyUI Desktop into our private dir and return the launchable
    path. Never touches /Applications or %LOCALAPPDATA%\\Programs -- the
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


def _kill_port_owner(port: int) -> None:
    """Best-effort kill of whatever process is bound to 127.0.0.1:<port>.
    Catches ComfyUI's Python backend (Documents/ComfyUI/.venv/Scripts/
    python.exe) which survives `taskkill /F /IM ComfyUI.exe` because its
    image name is plain python.exe, not ComfyUI.exe."""
    try:
        if sys.platform == "win32":
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                f"-ErrorAction SilentlyContinue | "
                f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess "
                f"-Force -ErrorAction SilentlyContinue }}"
            ], capture_output=True, timeout=10)
        else:
            subprocess.run(
                ["bash", "-c",
                 f"lsof -ti tcp:{port} -sTCP:LISTEN | xargs -r kill -9"],
                capture_output=True, timeout=10,
            )
    except Exception:
        pass


def _kill_existing(desktop_mode: str) -> None:
    """Kill any running ComfyUI process so our --remote-debugging-port flag takes effect.
    Also kills whoever's bound to port 8000 (the orphan ComfyUI Python
    backend from a half-killed prior run); without this, the new wizard
    click-through silently skips because /system_stats appears up at t=0."""
    if desktop_mode == "mac":
        subprocess.run(["pkill", "-f", "ComfyUI"], capture_output=True)
    else:
        subprocess.run(["taskkill", "/F", "/IM", "ComfyUI.exe"], capture_output=True)
    _kill_port_owner(8000)
    time.sleep(2)


def _resolve_user_profile() -> Path:
    """Real user profile root. USERPROFILE / USERNAME may point at the
    SYSTEM context when launched from agent harnesses or scheduled tasks;
    fall through to a C:\\Users\\* scan that finds the profile actually
    holding ComfyUI state."""
    up = os.environ.get("USERPROFILE", "")
    if up and "systemprofile" not in up.lower():
        return Path(up)
    name = os.environ.get("USERNAME", "")
    if name and name.upper() != "SYSTEM":
        p = Path("C:/Users") / name
        if p.exists():
            return p
    try:
        from glob import glob as _glob
        skip = ("default", "default user", "public", "all users")
        for p in _glob(r"C:\Users\*"):
            pp = Path(p)
            if pp.name.lower() in skip:
                continue
            if (pp / "AppData/Roaming/ComfyUI").exists() or (pp / "Documents/ComfyUI").exists():
                return pp
    except Exception:
        pass
    return Path.home()


def _force_rmtree(p: Path) -> None:
    """rmtree that clears the read-only flag .venv/pixi envs leave behind."""
    import stat as _stat

    def _onerror(func, path, _exc):
        try:
            os.chmod(path, _stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    if p.exists():
        shutil.rmtree(p, onerror=_onerror)


def _wipe_comfy_state() -> None:
    """Restore a 'bare Windows' baseline before each desktop run. Mirrors
    the docker fresh-container model: no ComfyUI install or user state
    survives between runs. Cached installer + harness venv are preserved
    (analogous to a docker base image being cached)."""
    profile = _resolve_user_profile()
    targets = [
        _CACHE_DIR / "ComfyUI",
        profile / "AppData" / "Roaming" / "ComfyUI",
        profile / "AppData" / "Local" / "Programs" / "ComfyUI",
        profile / "Documents" / "ComfyUI",
    ]
    for t in targets:
        if t.exists():
            print(f"[desktop] wipe: {t}", flush=True)
            _force_rmtree(t)


def _devtools_active_port_path(desktop_mode: str) -> Path:
    """Electron writes the chosen --remote-debugging-port to this file in
    its userData dir. Mirrors `_devtools_active_port_path` in cdp_driver.py;
    we resolve robustly against SYSTEM-context APPDATA so agent-harness
    shells don't trip us."""
    if desktop_mode == "mac":
        return (Path.home() / "Library" / "Application Support" /
                "ComfyUI" / "DevToolsActivePort")
    appdata = os.environ.get("APPDATA", "")
    if appdata and "systemprofile" not in appdata.lower():
        return Path(appdata) / "ComfyUI" / "DevToolsActivePort"
    up = os.environ.get("USERPROFILE", "")
    if up and "systemprofile" not in up.lower():
        return Path(up) / "AppData" / "Roaming" / "ComfyUI" / "DevToolsActivePort"
    username = os.environ.get("USERNAME", "")
    if username and username.upper() != "SYSTEM":
        return (Path("C:/Users") / username /
                "AppData" / "Roaming" / "ComfyUI" / "DevToolsActivePort")
    from glob import glob as _glob
    for p in _glob(r"C:\Users\*\AppData\Roaming\ComfyUI"):
        if "systemprofile" in p.lower():
            continue
        return Path(p) / "DevToolsActivePort"
    return Path.home() / "AppData" / "Roaming" / "ComfyUI" / "DevToolsActivePort"


def _launch(app_path: Path, desktop_mode: str, stdout_log: Path) -> None:
    """Launch the Desktop app with --remote-debugging-port=0; chromium picks
    a fresh ephemeral port the kernel guarantees is unbound, sidestepping
    the Windows orphan-LISTEN-socket problem completely. The chosen port
    is then read from <userData>/DevToolsActivePort by _wait_for_cdp."""
    # Clear any stale DevToolsActivePort from a prior instance so we don't
    # mistake its old port for the new one.
    devtools_file = _devtools_active_port_path(desktop_mode)
    try:
        devtools_file.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[desktop] DevToolsActivePort cleanup err (ignored): {e}",
              file=sys.stderr)

    out_fh = open(stdout_log, "wb")
    flag = "--remote-debugging-port=0"
    if desktop_mode == "mac":
        _open_mac_app(app_path, flag, out_fh)
    else:
        subprocess.Popen(
            [str(app_path), flag],
            stdout=out_fh, stderr=out_fh,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )


def _open_mac_app(app_path: Path, flag: str, out_fh) -> None:
    """`open <app> --args <flag>`, bridged into the user's aqua session via
    `sudo launchctl asuser <uid>` if we're in any SSH-spawned shell (incl.
    the loopback session limactl/colima keeps to the host's own sshd).
    Without the bridge, `open` succeeds but the app zombies in the Background
    launchd session: no Window Server, no CDP, no stdout."""
    cmd = ["open", str(app_path), "--args", flag]
    if not os.environ.get("SSH_CONNECTION"):
        # `open --args` forwards flags to the Electron main process argv.
        subprocess.Popen(cmd, stdout=out_fh, stderr=out_fh)
        return
    uid = str(os.getuid())
    # Try cached-creds sudo first so back-to-back runs are silent.
    probe = subprocess.run(
        ["sudo", "-n", "launchctl", "asuser", uid] + cmd,
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    # Cached creds expired (or never set): fall through to interactive sudo.
    print("[desktop] SSH_CONNECTION detected; bridging into aqua session "
          "via `sudo launchctl asuser`. Sudo may prompt for password.",
          file=sys.stderr)
    subprocess.run(
        ["sudo", "launchctl", "asuser", uid] + cmd,
        stdout=out_fh, stderr=out_fh, check=False,
    )


def _start_host_screencap(logs_dir: Path, desktop_mode: str):
    """macOS host-screen capture loop. Runs alongside ComfyUI Desktop from
    launch onward so the live monitor shows what's on the Mac screen
    (typically the first-run install wizard) before CDP comes up. Drops
    `host_NNNNNN.jpg` into the same frames dir cdp_driver.py writes to;
    monitor JS picks max index across both prefixes. Returns a Popen
    handle (or None) for cleanup."""
    if desktop_mode != "mac":
        return None
    frames_dir = logs_dir / "debug" / "electron_inspect" / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    debug_log = logs_dir / "debug" / "host-screencap.log"
    needs_bridge = bool(os.environ.get("SSH_CONNECTION"))
    uid = str(os.getuid())

    def _wrap(argv):
        """Wrap argv with the asuser bridge if we're SSH-spawned. Use plain
        `sudo` (NOT -n) so an expired-creds case prompts on the user's tty
        instead of silently failing."""
        if needs_bridge:
            return ["sudo", "launchctl", "asuser", uid] + argv
        return argv

    # Synchronous probe: one screencapture, fail loudly if it errors. This
    # is far easier to debug than a daemon loop with DEVNULL'd stderr.
    probe_path = frames_dir / "host_000000.jpg"
    probe = subprocess.run(
        _wrap(["/usr/sbin/screencapture", "-x", "-t", "jpg", "-T", "0",
               str(probe_path)]),
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or not probe_path.exists():
        print(f"[desktop] host-screencap: probe failed (rc={probe.returncode}). "
              f"stderr: {(probe.stderr or '').strip() or '(empty)'}",
              file=sys.stderr)
        return None

    # Probe worked -- start the loop, indices from 1. Any future failure goes
    # to debug_log so the user can read it.
    inner = (
        f'i=1; while sleep 1.5; do '
        f'/usr/sbin/screencapture -x -t jpg -T 0 '
        f'"{frames_dir}/host_$(printf %06d $i).jpg"; '
        f'i=$((i+1)); done'
    )
    cmd = _wrap(["/bin/bash", "-c", inner])
    try:
        log_fh = open(debug_log, "wb")
        p = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)
        atexit.register(lambda: p.terminate() if p.poll() is None else None)
        print(f"[desktop] host-screencap: writing host_*.jpg to {frames_dir} "
              f"(loop log: {debug_log})")
        return p
    except Exception as e:
        print(f"[desktop] host-screencap: skip ({e})", file=sys.stderr)
        return None


def _wait_for_cdp(desktop_mode: str, timeout_s: int = 240) -> Optional[int]:
    """Poll <userData>/DevToolsActivePort until chromium writes the chosen
    port. Returns the port (int) on success, None on timeout."""
    devtools_file = _devtools_active_port_path(desktop_mode)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if devtools_file.exists():
            try:
                content = devtools_file.read_text(encoding="utf-8").strip()
                if content:
                    port = int(content.splitlines()[0])
                    # Sanity-check: confirm chromium is actually listening.
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/json/version", timeout=2)
                        return port
                    except Exception:
                        pass
            except Exception:
                pass
        time.sleep(1)
    return None
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


_LIVE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>comfy-test live</title>
<style>
  html,body{margin:0;height:100%;background:#111;color:#ddd;
    font:13px/1.4 ui-monospace,Consolas,monospace}
  #wrap{display:flex;flex-direction:column;height:100vh}
  #img{flex:1 1 auto;min-height:0;width:100%;object-fit:contain;background:#000}
  #meta{padding:4px 10px;background:#222;border-top:1px solid #333;
    border-bottom:1px solid #333}
  #bottom{flex:0 0 32vh;display:flex;min-height:0}
  .pane{flex:1 1 50%;display:flex;flex-direction:column;min-width:0}
  #pwpane{border-right:1px solid #333}
  .label{padding:2px 8px;background:#1a1a1a;color:#888;
    border-bottom:1px solid #333;font-size:11px;letter-spacing:.05em;
    display:flex;align-items:center;justify-content:space-between}
  .copybtn{background:#222;color:#aaa;border:1px solid #333;border-radius:3px;
    padding:0 8px;font:11px ui-monospace,Consolas,monospace;cursor:pointer;
    letter-spacing:0}
  .copybtn:hover{background:#2a2a2a;color:#ddd}
  .copybtn.ok{color:#7c7;border-color:#3a4}
  .pre{flex:1 1 auto;overflow:auto;margin:0;padding:6px 10px;
    background:#000;white-space:pre-wrap;min-height:0}
</style></head><body>
<div id="wrap">
  <img id="img" alt="">
  <div id="meta">starting...</div>
  <div id="bottom">
    <div id="pwpane" class="pane">
      <div class="label"><span>> playwright (session.log)</span>
        <button class="copybtn" data-src="/session.log">copy</button></div>
      <pre id="pwlog" class="pre">(waiting for session.log)</pre>
    </div>
    <div id="comfypane" class="pane">
      <div class="label"><span>> comfy (comfyui.log)</span>
        <button class="copybtn" data-src="/comfy.log">copy</button></div>
      <pre id="comfylog" class="pre">(waiting for comfyui.log)</pre>
    </div>
  </div>
</div>
<script>
const FRAMES="/debug/electron_inspect/frames/",
      PW="/session.log", CL="/comfy.log";
const img=document.getElementById("img"),
      meta=document.getElementById("meta"),
      pwlog=document.getElementById("pwlog"),
      comfylog=document.getElementById("comfylog");
let last=-1;

function setTail(el, text, n){
  const tail=text.split(/\\r?\\n/).slice(-n).join("\\n");
  const stick=el.scrollTop+el.clientHeight+40>=el.scrollHeight;
  el.textContent=tail || "(empty)";
  if(stick) el.scrollTop=el.scrollHeight;
}

async function pollLog(url, el, n, label){
  try{
    const r=await fetch(url+"?t="+Date.now(),{cache:"no-store"});
    if(r.ok){ setTail(el, await r.text(), n); }
    else if(r.status===404){ el.textContent="("+label+" not yet available)"; }
  }catch(_){}
}

async function tick(){
  try{
    const r=await fetch(FRAMES,{cache:"no-store"});
    if(r.ok){
      const t=await r.text();
      let m=-1, bestPrefix="frame", bestExt="png";
      for(const x of t.matchAll(/(frame|host)_(\\d+)\\.(png|jpg)/g)){
        const n=parseInt(x[2],10);
        if(n>m){ m=n; bestPrefix=x[1]; bestExt=x[3]; }
      }
      if(m>last){
        img.src=FRAMES+bestPrefix+"_"+String(m).padStart(6,"0")+"."+bestExt+"?t="+Date.now();
        last=m;
      }
      meta.textContent=`${bestPrefix} ${m<0?"--":m} * ${new Date().toLocaleTimeString()}`;
    }else{
      meta.textContent="frames dir not yet available (HTTP "+r.status+")";
    }
  }catch(e){ meta.textContent="poll error: "+e; }
  pollLog(PW, pwlog, 30, "session.log");
  pollLog(CL, comfylog, 80, "comfyui.log");
}
tick(); setInterval(tick,500);

document.querySelectorAll(".copybtn").forEach(b=>{
  b.addEventListener("click", async ()=>{
    const url=b.dataset.src, prev=b.textContent;
    b.textContent="...";
    try{
      const r=await fetch(url+"?t="+Date.now(),{cache:"no-store"});
      if(!r.ok) throw new Error("HTTP "+r.status);
      const txt=await r.text();
      await navigator.clipboard.writeText(txt);
      b.textContent="OK copied"; b.classList.add("ok");
    }catch(e){
      b.textContent="FAIL "+(e.name||e.message||"error");
    }
    setTimeout(()=>{ b.textContent=prev; b.classList.remove("ok"); }, 1200);
  });
});
</script></body></html>
"""


def _resolve_comfy_log() -> Optional[Path]:
    # APPDATA is the obvious source, but agent harnesses / scheduled tasks
    # sometimes inherit a SYSTEM-profile env where APPDATA points at the
    # systemprofile subtree ComfyUI never writes to. Fall through to
    # USERPROFILE-, USERNAME-, then a glob across C:\Users\* before giving up.
    seen: set = set()
    candidates: list = []
    def add(p):
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(p)
    appdata = os.environ.get("APPDATA")
    if appdata:
        add(Path(appdata) / "ComfyUI" / "logs" / "comfyui.log")
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        add(Path(userprofile) / "AppData" / "Roaming" / "ComfyUI" / "logs" / "comfyui.log")
    username = os.environ.get("USERNAME")
    if username and username.upper() != "SYSTEM":
        add(Path("C:/Users") / username / "AppData" / "Roaming" / "ComfyUI" / "logs" / "comfyui.log")
    for c in candidates:
        if c.exists():
            return c
    try:
        from glob import glob as _glob
        skip = ("systemprofile", "default", "default user", "public", "all users")
        hits = []
        for p in _glob(r"C:\Users\*\AppData\Roaming\ComfyUI\logs\comfyui.log"):
            user_seg = Path(p).parts[2].lower() if len(Path(p).parts) > 2 else ""
            if user_seg in skip:
                continue
            pp = Path(p)
            if pp.exists():
                hits.append(pp)
        if hits:
            hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return hits[0]
    except Exception:
        pass
    return None


_comfy_log_logged = [False]


def _start_monitor_server(port: int, logs_dir: Path) -> None:
    """Best-effort daemon HTTP server on 127.0.0.1:<port> rooted at logs_dir.
    GET / returns the embedded live viewer; everything else is served as
    static files. Port collision is logged, not fatal."""
    import functools
    import http.server
    import socketserver
    import threading

    body = _LIVE_HTML.encode("utf-8")

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.split("?", 1)[0] == "/comfy.log":
                path = _resolve_comfy_log()
                if path is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    with path.open("rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 65536))
                        data = f.read()
                except FileNotFoundError:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                except Exception as e:
                    msg = f"comfy.log read error: {e}".encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                if not _comfy_log_logged[0]:
                    print(f"[desktop] monitor: comfy.log resolved to {path}",
                          flush=True)
                    _comfy_log_logged[0] = True
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            return super().do_GET()

        def log_message(self, *_a, **_k):
            pass  # silence per-request stderr spam

    handler = functools.partial(_Handler, directory=str(logs_dir))
    try:
        srv = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
    except OSError as e:
        print(f"[desktop] monitor: skip -- port {port} unavailable ({e})",
              file=sys.stderr)
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[desktop] monitor: http://127.0.0.1:{port}/  (live frame + session.log)")


def run_desktop(args, desktop_mode: str) -> int:
    """Local-host equivalent of the desktop YMLs. Returns process rc."""
    err = _validate_host(desktop_mode)
    if err:
        print(f"[desktop] {err}", file=sys.stderr)
        return 2

    if not _CDP_DRIVER.is_file():
        print(f"[desktop] cdp_driver.py not found at {_CDP_DRIVER}", file=sys.stderr)
        return 2

    # Bare-Windows baseline: kill any leftover ComfyUI + Python backend,
    # then wipe install + user state. Always-on; mirrors docker's
    # per-container freshness model.
    _kill_existing(desktop_mode)
    _wipe_comfy_state()

    # Auto-cleanup on exit so Ctrl+C / exception / normal exit ALL kill the
    # ComfyUI tree. Without this the Electron app + its Python backend
    # survive Ctrl+C and the next run hits a stale :8000 listener that
    # silently makes the wizard click-through skip.
    def _cleanup_comfy_processes(*_a):
        try:
            _kill_existing(desktop_mode)
        except Exception:
            pass
    atexit.register(_cleanup_comfy_processes)
    def _sig_cleanup(signum, _frame):
        _cleanup_comfy_processes()
        # Restore default handler and re-raise so the signal still terminates us.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGINT, _sig_cleanup)
    if hasattr(signal, "SIGTERM"):
        try: signal.signal(signal.SIGTERM, _sig_cleanup)
        except Exception: pass

    # Manager installs from main of the URL via the in-app GUI flow. We
    # still shallow-clone the node locally so we can enumerate workflows/*.json
    # from disk (avoids hitting api.github.com/repos/.../contents which the
    # macOS hosted-runner pool's NAT'd egress IPs frequently 403 with anon
    # rate-limit). cdp_driver picks up the list via COMFY_TEST_WORKFLOWS env.
    from comfy_test.cli._nodelink import clone_node, expand_nodelink

    # NOT .rstrip(".git") -- that strips ANY trailing chars in {'.','g','i','t'},
    # so "ComfyUI-UniRig.git" becomes "ComfyUI-UniR" and every downstream fetch
    # (pyproject.toml, comfy-test.toml, workflows/) 404s, leaving the run with
    # "no workflows ran" but rc=0.
    url = expand_nodelink(args.nodelink).removesuffix(".git")
    node_name = url.rsplit("/", 1)[-1]

    clone_root = Path(tempfile.mkdtemp(prefix="comfy-test-desktop-clone-"))
    atexit.register(lambda: shutil.rmtree(clone_root, ignore_errors=True))
    workflow_names: list[str] = []
    node_sha: Optional[str] = None
    try:
        clone_node(url, "main", clone_root, log_prefix="[desktop]")
        workflows_dir = clone_root / node_name / "workflows"
        if workflows_dir.is_dir():
            workflow_names = sorted(p.stem for p in workflows_dir.glob("*.json"))
        # Capture HEAD SHA so cdp_driver can write it as commit_hash in
        # results.json. Manager installs from main, so this is the SHA the
        # test actually ran against -- the dashboard compares it against
        # the node's main HEAD (not the dispatched branch).
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
        print(f"[desktop] clone failed (workflow enumeration will fall back "
              f"to api.github.com): {e}", file=sys.stderr)
    print(f"[desktop] node: {node_name}  (URL: {url}, branch: main, "
          f"sha: {node_sha[:12] if node_sha else 'unknown'}, "
          f"workflows: {workflow_names})")

    # Logs dir matches the cli/run.py shape: <run_id>/<branch>/<platform>/
    # so dispatch-test.yml's publish step finds results.json with the same
    # `find -path "*/<short>-*/<branch>/<platform>/results.json"` glob it
    # uses for cpu / gpu jobs.
    short = node_name.removeprefix("ComfyUI-")
    timestamp = datetime.now().strftime("%H%M")
    run_id = f"{short}-{timestamp}"
    branch_dir = getattr(args, "branch", None) or "main"
    platform_dir = {
        "mac":         "macos-desktop",
        "windows":     "windows-desktop",
        "windows_gpu": "windows-desktop-gpu",
    }.get(desktop_mode, desktop_mode)
    # Honor COMFY_TEST_LOGS_DIR when set (CI YML points it at
    # ${{ github.workspace }}/comfy-test-logs so the artifact upload step
    # finds the run dir). Fall back to ~/comfy-test-logs for local use.
    _env_logs = os.environ.get("COMFY_TEST_LOGS_DIR")
    logs_root = Path(_env_logs) if _env_logs else Path.home() / "comfy-test-logs"
    logs_dir = logs_root / run_id / branch_dir / platform_dir
    debug_dir = logs_dir / "debug"
    for d in (logs_dir, debug_dir,
              logs_dir / "logs", logs_dir / "screenshots", logs_dir / "videos"):
        d.mkdir(parents=True, exist_ok=True)
    (logs_dir / "crash_dump.log").touch()
    print(f"[desktop] logs: {logs_dir}")

    monitor_port = getattr(args, "monitor_progress", None)
    if monitor_port:
        _start_monitor_server(monitor_port, logs_dir)

    # Bootstrap an isolated venv with playwright + chromium + ffmpeg so the
    # host's system Python (or homebrew python) doesn't get touched.
    venv_python = _ensure_venv()

    # Bootstrap Desktop install + launch. (kill/wipe already ran up-front.)
    # Launch with --remote-debugging-port=0 so chromium picks a fresh
    # ephemeral port -- no fight with stale Windows orphan-LISTEN sockets
    # from prior killed runs. We read the chosen port from
    # <userData>/DevToolsActivePort.
    app_path = _ensure_desktop_app(desktop_mode)
    stdout_log = debug_dir / "electron_stdout.log"
    _launch(app_path, desktop_mode, stdout_log)
    screencap_proc = _start_host_screencap(logs_dir, desktop_mode)
    print(f"[desktop] launched {app_path}, waiting for DevToolsActivePort...")
    try:
        cdp_port = _wait_for_cdp(desktop_mode, 240)
    finally:
        # Stop host capture as soon as cdp_driver takes over (or we bail).
        # cdp_driver writes higher-indexed frame_*.png that the monitor JS
        # picks over our host_*.jpg from this point on.
        if screencap_proc is not None:
            try: screencap_proc.terminate()
            except Exception: pass
    if cdp_port is None:
        print(f"[desktop] CDP didn't come up within 240s "
              f"(no DevToolsActivePort)", file=sys.stderr)
        return 1
    print(f"[desktop] CDP up on :{cdp_port}; running cdp_driver.py via cached venv")

    # Drive the app via cdp_driver. Env vars match what the YMLs set.
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "COMFY_TEST_GPU": "1" if desktop_mode == "windows_gpu" else "0",
        "COMFY_TEST_LOGS_DIR": str(logs_dir),
        "COMFY_TEST_DEBUG_DIR": str(debug_dir),
        "NODE_REPO": url.rsplit("github.com/", 1)[-1],
        "NODE_BRANCH": "main",  # Desktop only ever installs from main.
        "NODE_NAME": node_name,
        # Pre-enumerated from the local clone above. cdp_driver's
        # _fetch_workflow_list_from_repo short-circuits on this and skips
        # the api.github.com call (which the macOS hosted-runner pool
        # frequently 403s with anonymous rate-limit).
        "COMFY_TEST_WORKFLOWS": ",".join(workflow_names),
        # cdp_driver writes these into results.json so the dashboard can
        # render the cell colored by pass/fail and match the cpu schema.
        "COMFY_TEST_NODE_SHA": node_sha or "",
        "COMFY_TEST_DESKTOP_PLATFORM": {
            "mac":         "macos_desktop",
            "windows":     "windows_desktop",
            "windows_gpu": "windows_desktop_gpu",
        }.get(desktop_mode, "unknown_desktop"),
        # cdp_driver's post-Apply-Changes relaunch picks the executable from
        # these. Without them it falls back to the CI-installed path.
        "COMFY_DESKTOP_APP_EXE": str(_APP_EXE),
        "COMFY_DESKTOP_APP_PATH": str(_APP_DIR),
        # cdp_driver uses this for its initial connect, post-relaunch
        # poll/reconnect, and the post-Apply-Changes app Popen flag.
        "COMFY_DESKTOP_CDP_PORT": str(cdp_port),
    })
    # Tee cdp_driver's stdout/stderr to BOTH session.log (for the artifact)
    # AND the parent's stdout (for live CI step log visibility). Also spawn
    # a background thread that tails ComfyUI's comfyui.log so the Python
    # backend's output (model loads, node execution, errors) shows up in
    # the step log too -- equivalent to what `--monitor-progress` shows
    # locally but going to stdout instead of an HTTP page.
    import threading
    session_log_path = logs_dir / "session.log"
    session_log = open(session_log_path, "w", encoding="utf-8", errors="replace")

    _comfy_tail_stop = threading.Event()
    def _tail_comfy_log():
        # Wait for the comfyui.log file to appear (ComfyUI may take 30s+
        # to bootstrap). Then tail it line-by-line, prefixing each line
        # with [comfy] so it's distinguishable from cdp_driver output.
        path = _resolve_comfy_log()
        deadline = time.time() + 600
        while path is None or not path.exists():
            if _comfy_tail_stop.is_set() or time.time() > deadline:
                return
            time.sleep(2)
            path = _resolve_comfy_log()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)  # tail -f start: end of file
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
            [str(venv_python), str(_CDP_DRIVER)],
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
