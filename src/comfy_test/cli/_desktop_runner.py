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
import json
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



# Desktop-mode token -> lane id. The ONE mapping; derived spellings come off it
# rather than being restated. Previously this map existed three times in two
# spellings (hyphenated for dirs, underscored for the env var), none of them
# checked against the registry.
_DESKTOP_MODE_LANES = {
    "mac": "macos-desktop",
    "windows": "windows-desktop",
    "windows_cuda": "windows-desktop-cuda",
}


def desktop_lane_id(desktop_mode: str) -> str:
    """Lane id for a desktop mode, validated against the lane registry."""
    lane = _DESKTOP_MODE_LANES.get(desktop_mode, desktop_mode)
    from comfy_test.lanes import BY_ID
    if lane not in BY_ID:
        raise ValueError(
            f"desktop mode {desktop_mode!r} maps to {lane!r}, which is not a "
            f"lane id. Known: {', '.join(sorted(BY_ID))}"
        )
    return lane


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
_DESKTOP_PKG = Path(__file__).resolve().parent.parent / "platforms" / "desktop"
_CDP_DRIVER = _DESKTOP_PKG / "cdp_driver.py"
_MERGE_LOGS = _DESKTOP_PKG / "merge_logs.py"

def _write_manager_security_config() -> None:
    """Pre-write ComfyUI-Manager's config.ini with `security_level = weak`
    + `allow_git_url_install = true`.

    Required by cdp_driver's install phase: when the Manager-UI clickthrough
    silently no-ops (empty CNR downloadUrl, network hiccup, etc.), driver
    falls back to /customnode/install/git_url. That endpoint checks BOTH
    security_level >= weak AND allow_git_url_install=true. Default config
    has security_level=normal + no allow_git_url_install, so we pre-seed.

    Manager's config path differs by ComfyUI version
    (manager_migration.py:45):
        - new (has_system_user_api): <user_dir>/__manager/config.ini
        - legacy:                    <user_dir>/default/ComfyUI-Manager/config.ini
    We write both -- whichever Manager picks up, the value is the same.

    Desktop's user_dir is <Documents>/ComfyUI/user/ on Mac and Windows.
    Called AFTER _wipe_comfy_state so the wipe doesn't take our config with it.
    """
    profile = _resolve_user_profile()
    user_dir = profile / "Documents" / "ComfyUI" / "user"
    paths = [
        user_dir / "__manager" / "config.ini",
        user_dir / "default" / "ComfyUI-Manager" / "config.ini",
    ]
    body = ("[default]\n"
            "security_level = weak\n"
            "allow_git_url_install = true\n")
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            print(f"[desktop] manager config: wrote security_level=weak + "
                  f"allow_git_url_install=true -> {p}",
                  flush=True)
        except Exception as e:
            print(f"[desktop] manager config: failed to write {p}: {e}",
                  file=sys.stderr, flush=True)


def _enable_manager_legacy_ui() -> None:
    """Add --enable-manager-legacy-ui to each standalone ComfyUI install's
    launchArgs in Comfy Desktop's installations.json.

    Why: as of Comfy Desktop 1.0.34, the bundled Manager's `glob/`
    variant (loaded by default) no longer exposes /customnode/install/git_url.
    The `legacy/` variant DOES expose it (line 1550 of legacy/manager_server.py)
    but only gets loaded when ComfyUI is launched with
    --enable-manager-legacy-ui. Without this flag, driver's git-URL install
    fallback hits 405 and we lose branch-pinned install of the node under test.

    First-run case: installations.json doesn't exist yet -- the file is
    written during the setup wizard. We catch it on the next run; not fatal
    since the primary install path is the Manager-UI clickthrough, which
    doesn't need the legacy UI.
    """
    # installations.json path:
    #   macOS:   ~/Library/Application Support/Comfy Desktop/installations.json
    #   Windows: %APPDATA%\Comfy Desktop\installations.json
    if sys.platform == "win32":
        # Not os.environ["APPDATA"] -- that can name the SYSTEM profile. Take
        # the first app-name variant that actually holds the file, falling
        # back to the current name for the first-run message below.
        roaming = Path(_user_env_overrides()["APPDATA"])
        settings_dir = next(
            (roaming / n for n in _APP_NAMES
             if (roaming / n / "installations.json").is_file()),
            roaming / _APP_NAMES[0])
    else:
        settings_dir = _resolve_user_profile() / "Library" / "Application Support" / "Comfy Desktop"
    installations = settings_dir / "installations.json"
    if not installations.is_file():
        print(f"[desktop] {installations} not present yet (first-run); "
              f"legacy-UI enable deferred to next launch",
              flush=True)
        return
    try:
        data = json.loads(installations.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[desktop] could not parse {installations}: {e}",
              file=sys.stderr, flush=True)
        return
    changed = False
    for inst in data if isinstance(data, list) else []:
        if inst.get("sourceId") != "standalone":
            continue
        args = inst.get("launchArgs", "")
        if "--enable-manager-legacy-ui" in args:
            continue
        inst["launchArgs"] = (args + " --enable-manager-legacy-ui").strip()
        changed = True
        print(f"[desktop] launchArgs: added --enable-manager-legacy-ui "
              f"for install {inst.get('id')}",
              flush=True)
    if changed:
        installations.write_text(json.dumps(data, indent=2), encoding="utf-8")


# All host-side state lives under here so a `dockertest --desktop_*` run
# leaves nothing behind on the host outside this dir (other than the
# ComfyUI Desktop's own runtime data dir at ~/Documents/ComfyUI which is
# managed by the app itself, not by us).
_CACHE_DIR = Path.home() / ".comfy-test-cache" / "desktop"
# The app used to ship as ComfyUI.app; recent Desktop builds rename it to
# "Comfy Desktop.app" (mounted at /Volumes/Comfy Desktop/). We cache under
# the new name; _ensure_desktop_app auto-detects the actual bundle name
# from the mount so future renames don't break the download step.
_APP_DIR = _CACHE_DIR / "Comfy Desktop.app"    # mac
_APP_INSTALL_DIR = _CACHE_DIR / "ComfyUI"      # windows: the NSIS /D= target
_VENV_DIR = _CACHE_DIR / "venv"

# CFBundleName / productName candidates used for Electron userData +
# electron-log dir names. Order matters -- newest first, then legacy. Any
# resolver that hunts for DevToolsActivePort or ComfyUI logs iterates this
# list so a rename doesn't require touching every call site.
_APP_NAMES = ("Comfy Desktop", "ComfyUI")


def _find_app_exe() -> Optional[Path]:
    """The installed Electron binary on Windows, or None if not installed.

    The NSIS build renamed the executable the same way the mac bundle was
    renamed (ComfyUI.exe -> "Comfy Desktop.exe"), so probe the known product
    names first, then fall back to any top-level .exe that isn't the
    uninstaller. Mirrors how the mac branch globs *.app instead of
    hardcoding a name.

    Searches the /D= target FIRST, then electron-builder's default per-user
    install roots: one-click NSIS installers are known to ignore /D and land
    in %LOCALAPPDATA%\\Programs\\<product> -- in the sandbox guest the
    installer exited 0 with the /D directory left completely empty
    (GeometryPack-1750).
    """
    roots = [_APP_INSTALL_DIR]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for name in _APP_NAMES:
            roots.append(Path(local) / "Programs" / name)
        roots.append(Path(local) / "Programs" / "comfyui-electron")
    for root in roots:
        if not root.is_dir():
            continue
        for name in _APP_NAMES:
            p = root / f"{name}.exe"
            if p.is_file():
                return p
        for p in sorted(root.glob("*.exe")):
            if not p.name.lower().startswith("uninstall"):
                return p
    return None


_DESKTOP_DOWNLOAD_URLS = {
    "mac":         "https://download.comfy.org/mac/dmg/arm64",
    "windows":     "https://download.comfy.org/windows/nsis/x64",
    "windows_cuda": "https://download.comfy.org/windows/nsis/x64",
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
    if desktop_mode in ("windows", "windows_cuda") and host != "windows":
        return f"--{desktop_mode.replace('_', '-')} requires a Windows host, got {host}"
    # SSH-spawned shells (incl. loopback ones, like the one limactl/colima
    # holds open against the host's own sshd) put the process in a Background
    # launchd session, where `open <app>` silently zombies. We auto-bridge at
    # the launch site via `sudo launchctl asuser <uid>`, so don't bail here.
    return None


def resolve_logs_dir_for_sandbox(node_name: str, node_branch: str,
                                 desktop_mode: str) -> Path:
    """The <logs_root>/<run_id>/<branch>/<platform>/ dir for a desktop run.

    Shared with the sandbox runner so the host can compute the same
    destination the guest wrote to, and the artifact tree keeps the shape
    dispatch-test.yml's publish glob expects:
    `*/<short>-*/<branch>/<platform>/results.json`.

    COMFY_TEST_RUN_DIR short-circuits everything: it names the EXACT run dir
    to use. The sandbox bootstrap sets it to the guest view of the host's
    pre-created D:\\logs\\<run-id>\\<branch>\\<platform> mapped folder, so the
    guest writes final artifacts directly (live logs on the host, no
    end-of-run collection, no duplicated <run-id> nesting, and no
    minute-boundary skew between host- and guest-minted run ids).
    """
    _exact = os.environ.get("COMFY_TEST_RUN_DIR")
    if _exact:
        return Path(_exact)
    short = node_name.removeprefix("ComfyUI-")
    run_id = f"{short}-{datetime.now().strftime('%H%M')}"
    platform_dir = desktop_lane_id(desktop_mode)
    # Honor COMFY_TEST_LOGS_DIR when set (CI YML points it at
    # ${{ github.workspace }}/comfy-test-logs so the artifact upload step
    # finds the run dir). Fall back to ~/comfy-test-logs for local use.
    _env_logs = os.environ.get("COMFY_TEST_LOGS_DIR")
    logs_root = Path(_env_logs) if _env_logs else Path.home() / "comfy-test-logs"
    return logs_root / run_id / node_branch / platform_dir


def _ensure_desktop_app(desktop_mode: str, refresh: bool = False) -> Path:
    """Cache ComfyUI Desktop into our private dir and return the launchable
    path. Never touches /Applications or %LOCALAPPDATA%\\Programs -- the
    whole point of `dockertest` is isolation, so the host stays clean.
    A subsequent run reuses the cached copy unless --refresh-app is passed.

    Returns the .app dir on macOS, the .exe path on Windows.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if refresh:
        stale = _APP_DIR if desktop_mode == "mac" else _APP_INSTALL_DIR
        if stale.exists():
            print(f"[desktop] --refresh-app: discarding cached app at {stale}")
            _force_rmtree(stale)
    if desktop_mode == "mac":
        if _APP_DIR.exists():
            print(f"[desktop] reusing cached app at {_APP_DIR}")
            return _APP_DIR
        dmg = _CACHE_DIR / "comfyui-desktop.dmg"
        _download(_DESKTOP_DOWNLOAD_URLS["mac"], dmg)
        # Mount, copy app, detach. The DMG's volume name has drifted across
        # releases: older builds mount at /Volumes/ComfyUI*, newer builds at
        # /Volumes/Comfy Desktop. Glob broadly and pick whichever appears.
        # The .app bundle inside has also been renamed (ComfyUI.app ->
        # "Comfy Desktop.app"), so we don't hardcode the name -- take the
        # first *.app in the mount.
        subprocess.run(["hdiutil", "attach", "-nobrowse", str(dmg)], check=True)
        try:
            mounts = (list(Path("/Volumes").glob("Comfy Desktop*")) +
                      list(Path("/Volumes").glob("ComfyUI*")))
            if not mounts:
                raise RuntimeError(
                    "No Comfy Desktop/ComfyUI volume under /Volumes after "
                    "hdiutil attach")
            mount = mounts[0]
            apps = list(mount.glob("*.app"))
            if not apps:
                raise RuntimeError(f"No .app bundle found in {mount}")
            src = apps[0]
            print(f"[desktop] copying {src} -> {_APP_DIR}")
            # cp -R preserves the framework symlinks
            # (Versions/Current -> A, top-level binary -> Versions/Current/Foo).
            # shutil.copytree defaults to symlinks=False which dereferences
            # them, materializing every framework Version as a full copy
            # and producing a bundle Gatekeeper rejects with
            # "bundle format is ambiguous (could be app or framework)".
            subprocess.run(["cp", "-R", str(src), str(_APP_DIR)], check=True)
        finally:
            for m in (list(Path("/Volumes").glob("Comfy Desktop*")) +
                      list(Path("/Volumes").glob("ComfyUI*"))):
                subprocess.run(["hdiutil", "detach", str(m)], capture_output=True)
        dmg.unlink(missing_ok=True)
        # Strip the quarantine xattr that Gatekeeper sets on downloaded
        # apps; otherwise first launch pops a "open anyway?" dialog the
        # CDP driver can't dismiss.
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(_APP_DIR)],
                       capture_output=True)
        return _APP_DIR

    # windows / windows_cuda
    cached = _find_app_exe()
    if cached is not None:
        print(f"[desktop] reusing cached app at {cached}")
        return cached
    setup = _CACHE_DIR / "ComfyUI-Setup.exe"
    _download(_DESKTOP_DOWNLOAD_URLS["windows"], setup)
    # NSIS supports /D for install dir. Use our cache root so the install
    # doesn't pollute %LOCALAPPDATA%\Programs\ComfyUI on the host.
    # One retry with a FRESH download: the installer itself crashed once with
    # 0xC0000005 right after a size-plausible download (GeometryPack-2242) --
    # a transient that otherwise costs the whole run.
    for _attempt in (1, 2):
        try:
            subprocess.run([str(setup), "/S", f"/D={_APP_INSTALL_DIR}"],
                           check=True)
            break
        except subprocess.CalledProcessError as e:
            if _attempt == 2:
                raise
            print(f"[desktop] installer failed (rc={e.returncode}); "
                  f"re-downloading and retrying once")
            setup.unlink(missing_ok=True)
            _download(_DESKTOP_DOWNLOAD_URLS["windows"], setup)
    for _ in range(180):
        found = _find_app_exe()
        if found is not None:
            return found
        time.sleep(1)
    # Report what actually landed EVERYWHERE we know to look, so the next
    # location change is self-diagnosing instead of another silent poll.
    report = []
    probes = [_APP_INSTALL_DIR]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        probes.append(Path(local) / "Programs")
    for root in probes:
        try:
            names = sorted(p.name for p in root.iterdir())
        except OSError:
            names = ["<unreadable>"]
        report.append(f"{root}: {', '.join(names) if names else '<nothing>'}")
    raise RuntimeError(
        f"No Comfy Desktop executable found after silent install "
        f"(tried {', '.join(n + '.exe' for n in _APP_NAMES)} and any "
        f"non-uninstaller *.exe under the /D target and "
        f"%LOCALAPPDATA%\\Programs). Contents: " + " | ".join(report))


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
             "import playwright, imageio_ffmpeg, tomli, websocket; print('ok')"],
            capture_output=True, text=True,
        )
        if ok.returncode == 0:
            print(f"[desktop] reusing venv at {_VENV_DIR}")
            return venv_python
        # Missing dep (usually just websocket-client on a stale cache) --
        # install without recreating the venv so we don't have to
        # re-download chromium.
        print(f"[desktop] venv at {_VENV_DIR} missing deps, top-up install")
        subprocess.run([str(venv_python), "-m", "pip", "install", "--quiet",
                        "playwright", "imageio-ffmpeg", "tomli", "websocket-client"],
                       check=True)
        return venv_python

    print(f"[desktop] creating venv at {_VENV_DIR}")
    import venv as _venv  # stdlib
    _venv.EnvBuilder(with_pip=True, clear=True).create(str(_VENV_DIR))
    subprocess.run([str(venv_python), "-m", "pip", "install", "--quiet",
                    "playwright", "imageio-ffmpeg", "tomli", "websocket-client"],
                   check=True)
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
    click-through silently skips because /system_stats appears up at t=0.

    Matches BOTH the legacy "ComfyUI" process name and the new
    "Comfy Desktop" one -- the executable/bundle rename means pkill -f
    "ComfyUI" no longer catches the Electron main process."""
    if desktop_mode == "mac":
        # pkill -f matches against the full argv. Alternation via extended
        # regex (default on BSD pkill) catches both names in one call.
        subprocess.run(["pkill", "-f", "Comfy Desktop|ComfyUI"],
                       capture_output=True)
    else:
        subprocess.run(["taskkill", "/F", "/IM", "ComfyUI.exe"], capture_output=True)
        subprocess.run(["taskkill", "/F", "/IM", "Comfy Desktop.exe"],
                       capture_output=True)
        # End the session-1 scheduled tasks too. They are deliberately not
        # deleted while running (deleting a running task kills its process
        # tree, which would take the app down mid-test), so a run that was
        # killed rather than exited leaves them alive -- and the capture loop
        # then keeps writing screenshots into the PREVIOUS run's directory.
        for tn in (_SESSION_TASK_NAME, _SCREENCAP_TASK_NAME):
            subprocess.run(["schtasks", "/end", "/tn", tn],
                           check=False, capture_output=True, text=True)
        _kill_stray_screencap_processes()
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


def _user_env_overrides() -> dict:
    """APPDATA/LOCALAPPDATA corrected to the real user profile.

    Both can point at the SYSTEM profile when comfy-test is launched from an
    agent harness or a scheduled task. Electron derives its userData dir from
    APPDATA, so an uncorrected value sends DevToolsActivePort to
    C:\\Windows\\system32\\config\\systemprofile\\... -- exactly where
    _devtools_active_port_candidates deliberately refuses to look, which
    turns into a silent _wait_for_cdp timeout.
    """
    profile = _resolve_user_profile()
    return {
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
    }


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
    """Restore a bare-OS baseline before each desktop run. Mirrors the
    docker fresh-container model: no ComfyUI install or user state
    survives between runs. Cached installer + harness venv are preserved
    (analogous to a docker base image being cached).

    Covers BOTH the legacy 'ComfyUI' and new 'Comfy Desktop' app-name
    conventions so a re-run after the rename doesn't leave the old
    wizard-already-done state behind."""
    targets: list[Path] = []
    if sys.platform == "darwin":
        home = Path.home()
        for name in _APP_NAMES:
            targets += [
                home / "Library" / "Application Support" / name,
                home / "Library" / "Logs" / name,
                home / "Library" / "Preferences" / f"com.electron.{name}.plist",
                home / "Documents" / name,
            ]
        # Wipe stale ComfyUI-Installs -- Comfy Desktop increments its
        # `ComfyUI (N)` counter every fresh setup, orphaning ~2.5 GB per
        # run. Since we also wipe Application Support/Comfy Desktop above
        # (which holds installations.json), there's no active install to
        # preserve -- next launch just creates ComfyUI (N+1) fresh.
        targets += [home / "ComfyUI-Installs"]
    else:
        profile = _resolve_user_profile()
        # NB: _APP_INSTALL_DIR (the cached app itself) is deliberately NOT a
        # target. Wiping it defeated _ensure_desktop_app's reuse fast path,
        # so every Windows run re-downloaded ~150MB and re-ran the NSIS
        # installer. macOS never wiped its _APP_DIR either. The isolation
        # contract is about ComfyUI state, not about reinstalling Electron;
        # --refresh-app forces a clean reinstall when that's what you want.
        for name in _APP_NAMES:
            targets += [
                profile / "AppData" / "Roaming" / name,
                profile / "AppData" / "Local" / "Programs" / name,
                profile / "Documents" / name,
            ]
        # Windows equivalent of mac's ~/ComfyUI-Installs wipe above. Comfy
        # Desktop increments a `ComfyUI (N)` slot on every fresh setup, so
        # without this each run orphans ~2.5GB -- measured at 81GB across 15
        # slots before this was added.
        #
        # Note the dir is "Comfy-Desktop" with a HYPHEN, under LocalAppData.
        # That is a third app-name variant, absent from _APP_NAMES ("Comfy
        # Desktop", "ComfyUI"), which is why the loop above never matched it.
        #
        # Deliberately NOT wiping the sibling ComfyUI-Shared (models) or
        # ComfyUI-Cache dirs: mac leaves its equivalents alone, and clearing
        # them would force a full model re-download every run.
        targets += [
            profile / "AppData" / "Local" / "Comfy-Desktop" / "ComfyUI-Installs",
        ]
    for t in targets:
        if t.exists():
            print(f"[desktop] wipe: {t}", flush=True)
            if t.is_dir():
                _force_rmtree(t)
            else:
                try:
                    t.unlink()
                except Exception:
                    pass


def _devtools_active_port_candidates(desktop_mode: str) -> list[Path]:
    """All plausible DevToolsActivePort locations across app-name variants
    and env-var context. Callers try each in order and take the first that
    exists (or use candidates[0] as the write-target for cleanup)."""
    out: list[Path] = []
    if desktop_mode == "mac":
        for name in _APP_NAMES:
            out.append(Path.home() / "Library" / "Application Support" /
                       name / "DevToolsActivePort")
        return out
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA", "")
    if appdata and "systemprofile" not in appdata.lower():
        roots.append(Path(appdata))
    up = os.environ.get("USERPROFILE", "")
    if up and "systemprofile" not in up.lower():
        roots.append(Path(up) / "AppData" / "Roaming")
    username = os.environ.get("USERNAME", "")
    if username and username.upper() != "SYSTEM":
        roots.append(Path("C:/Users") / username / "AppData" / "Roaming")
    from glob import glob as _glob
    for name in _APP_NAMES:
        for pattern in (rf"C:\Users\*\AppData\Roaming\{name}",):
            for p in _glob(pattern):
                if "systemprofile" not in p.lower():
                    roots.append(Path(p).parent)
                    break
    if not roots:
        roots.append(Path.home() / "AppData" / "Roaming")
    for root in roots:
        for name in _APP_NAMES:
            out.append(root / name / "DevToolsActivePort")
    # Dedupe while preserving order.
    seen: set = set()
    deduped: list[Path] = []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            deduped.append(p)
    return deduped


def _devtools_active_port_path(desktop_mode: str) -> Path:
    """Preferred (newest) DevToolsActivePort location. Used as the write
    target for pre-launch cleanup; readers should iterate the full
    candidate list from _devtools_active_port_candidates()."""
    return _devtools_active_port_candidates(desktop_mode)[0]


_SESSION_TASK_NAME = "comfy-test-desktop"

def _current_session_id() -> Optional[int]:
    """Windows session this process is in, or None off-Windows / on error.

    0 is the non-interactive services session. SSH and JupyterLab both land
    there, and everything they spawn inherits it.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(
                ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return None


_SCREENCAP_TASK_NAME = "comfy-test-screencap"

# Windows counterpart of the macOS /usr/sbin/screencapture loop in
# _start_host_screencap. Photographs the SCREEN rather than asking chromium to
# present a surface, which is the whole point: on this host the Electron window
# renders black and every CDP capture hangs, so the OS-level grab is the only
# thing that sees anything at all.
#
# Same output contract as macOS -- host_%06d.jpg into the frames dir -- so the
# live viewer needs no change: its JS already takes the max index across both
# `host_*` and `frame_*` prefixes.
#
# MUST run in session 1: session 0 has no desktop to photograph.
_SCREENCAP_PS1 = r'''param([string]$Dir, [int]$Fps = 1)
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
New-Item -ItemType Directory -Path $Dir -Force | Out-Null
# JPEG at ~70% keeps a long run's frames to a sane size; the report only ever
# shows these as thumbnails or a timelapse.
$codec = [Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
         Where-Object { $_.MimeType -eq 'image/jpeg' }
$eps = New-Object Drawing.Imaging.EncoderParameters 1
$eps.Param[0] = New-Object Drawing.Imaging.EncoderParameter(
    [Drawing.Imaging.Encoder]::Quality, [long]70)
$i = 0
$interval = [int](1000 / [Math]::Max(1, $Fps))
while ($true) {
    try {
        $b = [Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object Drawing.Bitmap $b.Width, $b.Height
        $g = [Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
        $i++
        $bmp.Save((Join-Path $Dir ('host_{0:D6}.jpg' -f $i)), $codec, $eps)
        $g.Dispose(); $bmp.Dispose()
    } catch { }
    Start-Sleep -Milliseconds $interval
}
'''


def _start_host_screencap_windows(logs_dir: Path) -> None:
    """Start the session-1 screen-capture loop. Best effort, never fatal."""
    frames_dir = logs_dir / "debug" / "electron_inspect" / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    script = logs_dir / "debug" / "screencap.ps1"
    script.write_text(_SCREENCAP_PS1, encoding="ascii")
    user = _resolve_user_profile().name
    cmd = (f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File '
           f'"{script}" -Dir "{frames_dir}" -Fps 1')
    # Kill any capture left over from a previous run FIRST. `/create /f`
    # rewrites the task definition but does NOT touch an already-running
    # instance, and `/run` will not start a second one -- so a survivor keeps
    # looping with its ORIGINAL -Dir and quietly files every screenshot under
    # the previous run, leaving this one with zero. That is not hypothetical:
    # a leftover from CADabra-1058 did exactly this to CADabra-1103.
    _stop_host_screencap_windows()   # also kills stray loops from killed runs
    try:
        subprocess.run(["schtasks", "/create", "/tn", _SCREENCAP_TASK_NAME, "/f",
                        "/sc", "once", "/st", "23:59", "/ru", user, "/it",
                        "/tr", cmd], check=True, capture_output=True, text=True)
        subprocess.run(["schtasks", "/run", "/tn", _SCREENCAP_TASK_NAME],
                       check=True, capture_output=True, text=True)
    except Exception as e:
        print(f"[desktop] host-screencap: skip ({e})", file=sys.stderr)
        return
    # This loop runs forever by design, so make very sure it dies with us --
    # leftover background tasks have already corrupted several runs.
    atexit.register(_stop_host_screencap_windows)
    print(f"[desktop] host-screencap: writing host_*.jpg to {frames_dir} "
          f"(session 1, 1fps)")


def _kill_stray_screencap_processes() -> None:
    """Kill capture loops that outlived their run.

    `schtasks /end` asks Task Scheduler to stop the task, but a run that was
    killed rather than exited cleanly never reached the atexit hook, and the
    powershell loop then survives indefinitely -- writing frames into a stale
    run's directory. Match on the script name so we only ever touch our own.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
             "Where-Object { $_.CommandLine -like '*screencap.ps1*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, text=True, timeout=30)
        for line in (r.stdout or "").split():
            if line.strip().isdigit():
                subprocess.run(["taskkill", "/F", "/PID", line.strip()],
                               check=False, capture_output=True, text=True)
    except Exception:
        pass


def _stop_host_screencap_windows() -> None:
    for args in (["schtasks", "/end", "/tn", _SCREENCAP_TASK_NAME],
                 ["schtasks", "/delete", "/tn", _SCREENCAP_TASK_NAME, "/f"]):
        try:
            subprocess.run(args, check=False, capture_output=True, text=True)
        except Exception:
            pass
    _kill_stray_screencap_processes()


def _launch_in_interactive_session(app_path: Path, flags: list, stdout_log: Path) -> None:
    """Start the Electron app on the interactive desktop via Task Scheduler.

    Why this exists: session 0 (where ssh/jupyter put us) has no Desktop
    Window Manager and is pinned by Windows to 1024x768, so Page.
    captureScreenshot stalls forever waiting for a frame that is never
    presented, and every frame_*.png comes back empty. schtasks /it runs the
    task with TASK_LOGON_INTERACTIVE_TOKEN, i.e. on the logged-on user's
    desktop.

    Only the GUI process moves. cdp_driver, orchestration and the live viewer
    stay in session 0 and reach the app over 127.0.0.1:<cdp port>, which is
    session-agnostic.
    """
    profile = _resolve_user_profile()
    user = profile.name
    # A .cmd wrapper keeps schtasks out of the quoting business (the flags
    # contain '*' and '=') and gives us the stdout redirect that a bare
    # /tr cannot.
    #
    # Launch the app and NOTHING ELSE. We used to also start a size-window.ps1
    # helper here that hunted for the app's HWND and resized / SW_SHOW'd /
    # foregrounded it. Every "the screen is black" symptom traced back to that
    # helper picking the wrong window: Comfy Desktop keeps two large
    # Chrome_WidgetWin windows alive, one of which it deliberately keeps
    # hidden, and the helper force-showed whichever it happened to find first
    # -- then in one run resized it to full-screen, covering the desktop.
    #
    # The app manages its own windows correctly when left alone. Screen
    # visibility comes from the host screencap loop (a plain CopyFromScreen at
    # 1fps), which photographs whatever is actually on screen and needs no
    # knowledge of window handles at all.
    wrapper = stdout_log.parent / "launch-desktop.cmd"
    quoted = " ".join(f'"{f}"' if " " in f else f for f in flags)
    wrapper.write_text(
        "@echo off\r\n"
        f'"{app_path}" {quoted} > "{stdout_log}" 2>&1\r\n',
        encoding="ascii",
    )
    # /f overwrites any task left behind by a previous run. We deliberately do
    # not delete it afterwards: schtasks /delete on a *running* task kills the
    # process tree, which would take the app down with it.
    create = ["schtasks", "/create", "/tn", _SESSION_TASK_NAME, "/f",
              "/sc", "once", "/st", "23:59", "/ru", user, "/it",
              "/tr", str(wrapper)]
    r = subprocess.run(create, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"could not register interactive launch task as {user!r}: "
            f"{(r.stderr or r.stdout).strip()}")
    r = subprocess.run(["schtasks", "/run", "/tn", _SESSION_TASK_NAME],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"could not start interactive launch task: "
            f"{(r.stderr or r.stdout).strip()}")
    print(f"[desktop] launched on the interactive desktop (session != 0) as "
          f"{user} via scheduled task {_SESSION_TASK_NAME!r}")


def _launch(app_path: Path, desktop_mode: str, stdout_log: Path) -> None:
    """Launch the Desktop app with --remote-debugging-port=0; chromium picks
    a fresh ephemeral port the kernel guarantees is unbound, sidestepping
    the Windows orphan-LISTEN-socket problem completely. The chosen port
    is then read from <userData>/DevToolsActivePort by _wait_for_cdp."""
    # Clear any stale DevToolsActivePort from a prior instance so we don't
    # mistake its old port for the new one. Wipe under every candidate
    # app-name userData dir; the app itself decides which it uses.
    for devtools_file in _devtools_active_port_candidates(desktop_mode):
        try:
            devtools_file.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[desktop] DevToolsActivePort cleanup err (ignored): {e}",
                  file=sys.stderr)

    out_fh = open(stdout_log, "wb")
    # --remote-allow-origins=* is required since chromium 111: without it, any
    # WS client whose Origin header isn't in the allowlist gets HTTP 403 on
    # the CDP upgrade. Playwright's connect_over_cdp happens to send an
    # allowlisted Origin so it works without the flag, but our live-viewer
    # (websocket-client) sends the CDP HTTP endpoint's own origin, which
    # chromium rejects. Passing `*` here matches what playwright's own
    # `chromium.launch()` does under the hood.
    flags = [
        "--remote-debugging-port=0", "--remote-allow-origins=*",
        # Keep the compositor committing frames when the window is occluded,
        # backgrounded or simply idle. Without these, chromium throttles the
        # renderer and Page.captureScreenshot never returns -- which surfaces
        # as `page.screenshot: Timeout 30000ms exceeded` after "fonts loaded",
        # and as zero frame_*.png for the whole run. Playwright sets all three
        # itself (see its driver's server/electron/loader.js); we launch
        # Electron by hand, so we have to pass them ourselves.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        # Windows-specific and distinct from the three above: chromium runs a
        # native occlusion detector that marks a covered HWND as occluded and
        # stops producing frames for it, regardless of the backgrounding flags.
        # Comfy Desktop keeps a second, empty top-level window around that can
        # sit over the app window, so this is reachable in normal operation.
        # No frames -> requestAnimationFrame stops -> playwright's "stable"
        # actionability check can never pass and EVERY click times out, which
        # is what made all 21 workflows report `timeout`. Unlike --disable-gpu
        # this does not change the compositing path, only the occlusion policy.
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    # DO NOT add --disable-gpu / --disable-gpu-compositing here. It was tried
    # (theory: a WebGL canvas that cannot present blocks the page's compositor
    # frame) and it makes Comfy Desktop EXIT a few seconds after the wizard:
    # run 2153 died with `connect_over_cdp: ECONNREFUSED` at ~153s and left no
    # crash trace, where run 2145 -- identical but for these flags -- ran on
    # past 166s. Software compositing is not a safe swap for this app.
    if desktop_mode == "mac":
        _open_mac_app(app_path, flags, out_fh)
    elif _current_session_id() == 0:
        # Session 0 has no desktop and Windows pins it to 1024x768, so an
        # Electron window launched here can never be composited or captured.
        # Bounce the GUI into the interactive console session instead. This is
        # the Windows analogue of the `launchctl asuser` bridge _open_mac_app
        # already does for SSH-spawned shells on macOS.
        _launch_in_interactive_session(app_path, flags, stdout_log)
    else:
        subprocess.Popen(
            [str(app_path), *flags],
            stdout=out_fh, stderr=out_fh,
            env={**os.environ, **_user_env_overrides()},
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )


def _open_mac_app(app_path: Path, flags: list, out_fh) -> None:
    """`open <app> --args <flag>`, bridged into the user's aqua session via
    `sudo launchctl asuser <uid>` if we're in any SSH-spawned shell (incl.
    the loopback session limactl/colima keeps to the host's own sshd).
    Without the bridge, `open` succeeds but the app zombies in the Background
    launchd session: no Window Server, no CDP, no stdout."""
    cmd = ["open", str(app_path), "--args", *flags]
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
    handle (or None) for cleanup.

    Windows takes the equivalent path via _start_host_screencap_windows():
    same host_*.jpg contract, but driven by a session-1 scheduled task rather
    than a Popen, so it returns None and cleans up through atexit instead."""
    if desktop_mode != "mac":
        if sys.platform == "win32":
            _start_host_screencap_windows(logs_dir)
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
        f'i=1; while sleep 1; do '
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
    """Poll every candidate <userData>/DevToolsActivePort until chromium
    writes the chosen port. Returns the port (int) on success, None on
    timeout. Tries multiple app-name variants so a rename (ComfyUI ->
    Comfy Desktop) doesn't leave us waiting on the wrong file."""
    candidates = _devtools_active_port_candidates(desktop_mode)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for devtools_file in candidates:
            if not devtools_file.exists():
                continue
            try:
                content = devtools_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                port = int(content.splitlines()[0])
                # Sanity-check: confirm chromium is actually listening.
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2)
                    print(f"[desktop] DevToolsActivePort resolved to "
                          f"{devtools_file} (port {port})")
                    return port
                except Exception:
                    pass
            except Exception:
                pass
        time.sleep(1)
    return None


def _collect_logs(desktop_mode: str, dest: Path) -> None:
    """Copy ComfyUI Desktop's runtime logs into dest. Iterates both the
    legacy ('ComfyUI') and current ('Comfy Desktop') app-name variants."""
    dest.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    if desktop_mode == "mac":
        home = Path.home()
        for name in _APP_NAMES:
            sources += [
                home / "Documents" / name / "user",
                home / "Library" / "Logs" / name,
                home / "Library" / "Application Support" / name / "logs",
            ]
    else:
        appdata = Path(_user_env_overrides()["APPDATA"])
        for name in _APP_NAMES:
            sources.append(appdata / name / "logs")
    # Modern Comfy Desktop writes its backend log to a per-install path
    # (macOS default: ~/ComfyUI-Installs/<slot>/logs/comfyui.log; Windows
    # default: %LOCALAPPDATA%\Programs\Comfy Desktop\..., user-chosen).
    # Ask installations.json for the authoritative slot rather than
    # guessing -- that's how cdp_driver's live-tailer resolves it too.
    try:
        # install_paths, NOT cdp_driver: importing that script runs the whole
        # desktop test (a 1200s wizard walk, then sys.exit) and SystemExit is
        # not an Exception, so the except below would not catch it.
        from comfy_test.platforms.desktop.install_paths import find_active_comfy_install
        _install_path, _comfy_root, _, _ = find_active_comfy_install()
        sources.append(_install_path / "logs")
        sources.append(_comfy_root / "user")
    except Exception:
        # Fall back to the macOS default glob when installations.json
        # isn't readable (e.g. Desktop never launched -- nothing to collect
        # anyway, but the fallback keeps prior behavior).
        sources += list(Path.home().glob("ComfyUI-Installs/*/logs"))
        sources += list(Path.home().glob("ComfyUI-Installs/*/ComfyUI/user"))
    for src in sources:
        if not src.is_dir():
            continue
        for p in src.rglob("*.log"):
            try:
                shutil.copy2(p, dest / p.name)
            except Exception:
                pass


def _generate_index(logs_dir: Path, node_repo: str, desktop_mode: str,
                    dev: bool = False) -> None:
    """Render per-platform index.html into logs_dir using the framework's
    own report generator. Skips with a warning on import error so a missing
    optional dep doesn't fail the whole run.

    `dev` is accepted for backward compat with existing call sites but
    ignored -- --desktop and --desktop --dev share the same platform id;
    branch separation happens at the run-dir level (branch subdir).
    """
    try:
        from comfy_test.reporting.html_report import generate_html_report
        generate_html_report(logs_dir, repo_name=node_repo)
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
  #meta{padding:4px 10px;background:#222;border-top:1px solid #333}
  #bottom{flex:0 0 40vh;display:flex;flex-direction:column;min-height:0}
  #tabs{display:flex;background:#1a1a1a;border-top:1px solid #333;
    border-bottom:1px solid #333;align-items:stretch}
  .tab{background:transparent;color:#888;border:0;border-right:1px solid #333;
    padding:6px 14px;font:12px ui-monospace,Consolas,monospace;cursor:pointer;
    letter-spacing:.05em}
  .tab:hover{background:#222;color:#ccc}
  .tab.active{background:#000;color:#7cf}
  .spacer{flex:1 1 auto}
  .copybtn{background:#222;color:#aaa;border:1px solid #333;border-radius:3px;
    padding:0 8px;margin:3px 6px;font:11px ui-monospace,Consolas,monospace;
    cursor:pointer;letter-spacing:0}
  .copybtn:hover{background:#2a2a2a;color:#ddd}
  .copybtn.ok{color:#7c7;border-color:#3a4}
  #log{flex:1 1 auto;overflow:auto;margin:0;padding:6px 10px;background:#000;
    white-space:pre-wrap;min-height:0}
</style></head><body>
<div id="wrap">
  <img id="img" alt="">
  <div id="meta">starting...</div>
  <div id="bottom">
    <div id="tabs">
      <button class="tab active" data-src="/session.log" data-label="session.log">actions</button>
      <button class="tab" data-src="/electron.log" data-label="electron">electron</button>
      <button class="tab" data-src="/comfy.log" data-label="comfyui.log">comfy</button>
      <div class="spacer"></div>
      <button class="copybtn">copy</button>
    </div>
    <pre id="log">(waiting...)</pre>
  </div>
</div>
<script>
const FRAMES="/debug/electron_inspect/frames/";
const img=document.getElementById("img"),
      meta=document.getElementById("meta"),
      logEl=document.getElementById("log");
let last=-1;
let activeSrc="/session.log";
let activeLabel="session.log";

function setTail(el, text, n){
  const tail=text.split(/\\r?\\n/).slice(-n).join("\\n");
  const stick=el.scrollTop+el.clientHeight+40>=el.scrollHeight;
  el.textContent=tail || "(empty)";
  if(stick) el.scrollTop=el.scrollHeight;
}

async function pollLog(){
  try{
    const r=await fetch(activeSrc+"?t="+Date.now(),{cache:"no-store"});
    if(r.ok){ setTail(logEl, await r.text(), 200); }
    else if(r.status===404){ logEl.textContent="("+activeLabel+" not yet available)"; }
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
  pollLog();
}
tick(); setInterval(tick,500);

document.querySelectorAll(".tab").forEach(t=>{
  t.addEventListener("click", ()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    activeSrc=t.dataset.src;
    activeLabel=t.dataset.label;
    logEl.textContent="(loading "+activeLabel+"...)";
    pollLog();
  });
});

document.querySelector(".copybtn").addEventListener("click", async (ev)=>{
  const btn=ev.currentTarget, prev=btn.textContent;
  btn.textContent="...";
  try{
    const r=await fetch(activeSrc+"?t="+Date.now(),{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const txt=await r.text();
    await navigator.clipboard.writeText(txt);
    btn.textContent="OK copied"; btn.classList.add("ok");
  }catch(e){
    btn.textContent="FAIL "+(e.name||e.message||"error");
  }
  setTimeout(()=>{ btn.textContent=prev; btn.classList.remove("ok"); }, 1200);
});
</script></body></html>
"""


def _resolve_comfy_log() -> Optional[Path]:
    # APPDATA is the obvious source, but agent harnesses / scheduled tasks
    # sometimes inherit a SYSTEM-profile env where APPDATA points at the
    # systemprofile subtree ComfyUI never writes to. Fall through to
    # USERPROFILE-, USERNAME-, then a glob across C:\Users\* before giving up.
    # macOS: the backend log lives under the app's Documents dir chosen by
    # the user (defaults to ~/Documents/<AppName>/user/comfyui.log).
    seen: set = set()
    candidates: list = []
    def add(p):
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(p)
    if sys.platform == "darwin":
        home = Path.home()
        for name in _APP_NAMES:
            add(home / "Documents" / name / "user" / "comfyui.log")
            add(home / "Library" / "Logs" / name / "comfyui.log")
            add(home / "Library" / "Application Support" / name / "logs" / "comfyui.log")
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            for name in _APP_NAMES:
                add(Path(appdata) / name / "logs" / "comfyui.log")
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            for name in _APP_NAMES:
                add(Path(userprofile) / "AppData" / "Roaming" / name / "logs" / "comfyui.log")
        username = os.environ.get("USERNAME")
        if username and username.upper() != "SYSTEM":
            for name in _APP_NAMES:
                add(Path("C:/Users") / username / "AppData" / "Roaming" / name / "logs" / "comfyui.log")
    for c in candidates:
        if c.exists():
            return c
    if sys.platform != "darwin":
        try:
            from glob import glob as _glob
            skip = ("systemprofile", "default", "default user", "public", "all users")
            hits = []
            for name in _APP_NAMES:
                pattern = rf"C:\Users\*\AppData\Roaming\{name}\logs\comfyui.log"
                for p in _glob(pattern):
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
_electron_log_logged = [False]


def _resolve_electron_log(logs_dir: Path) -> Optional[Path]:
    """Locate the live Electron log for the running ComfyUI Desktop.

    electron-log's default paths are keyed on app.getName() (== productName
    == 'Comfy Desktop' in current builds, 'ComfyUI' in legacy):
      - macOS:   ~/Library/Logs/<name>/main.log
      - Windows: %APPDATA%\\<name>\\logs\\main.log

    The main-process log is what we want -- it captures the app's own
    lifecycle (window creation, IPC, --remote-debugging-port arg, crash
    stacks) that the CDP driver can't see. Falls back to
    debug/electron_stdout.log which is populated by our Popen redirect on
    Windows but is typically empty on macOS (`open` doesn't preserve the
    child's stdout)."""
    candidates: list[Path] = []
    if sys.platform == "darwin":
        home = Path.home()
        for name in _APP_NAMES:
            candidates.append(home / "Library" / "Logs" / name / "main.log")
            candidates.append(home / "Library" / "Logs" / name / "renderer.log")
    elif sys.platform == "win32":
        roots: list[Path] = []
        appdata = os.environ.get("APPDATA")
        if appdata and "systemprofile" not in appdata.lower():
            roots.append(Path(appdata))
        up = os.environ.get("USERPROFILE", "")
        if up and "systemprofile" not in up.lower():
            roots.append(Path(up) / "AppData" / "Roaming")
        for root in roots:
            for name in _APP_NAMES:
                candidates.append(root / name / "logs" / "main.log")
    # Always fall back to our Popen-captured stdout as a last resort.
    candidates.append(logs_dir / "debug" / "electron_stdout.log")
    for c in candidates:
        if c.exists():
            return c
    return None


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
            if self.path.split("?", 1)[0] == "/electron.log":
                path = _resolve_electron_log(logs_dir)
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
                    msg = f"electron.log read error: {e}".encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                if not _electron_log_logged[0]:
                    print(f"[desktop] monitor: electron.log resolved to {path}",
                          flush=True)
                    _electron_log_logged[0] = True
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
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
    # We relay ComfyUI's log and cdp_driver's output to this console, which on
    # Windows is cp1252. Keep its encoding (switching to UTF-8 would just
    # mojibake) but stop an unrepresentable character from raising
    # UnicodeEncodeError and killing the run. Scoped to desktop runs rather
    # than done at import, so importing this module has no global side effect.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    err = _validate_host(desktop_mode)
    if err:
        print(f"[desktop] {err}", file=sys.stderr)
        return 2

    # windows_cuda runs inside Windows Sandbox: it executes each node's
    # requirements.txt and install.py with Manager's security_level=weak, so
    # it needs a blast-radius boundary the host cannot provide. The CPU path
    # (`windows`) stays on the host, where GitHub-hosted runners already give
    # us disposability for free.
    #
    # COMFY_TEST_IN_SANDBOX is set by the guest bootstrap script; without this
    # guard the guest would recurse and try to launch a nested sandbox.
    if desktop_mode == "windows_cuda" and not os.environ.get("COMFY_TEST_IN_SANDBOX"):
        from .sandbox.run import run_in_sandbox
        return run_in_sandbox(args, desktop_mode)

    if not _CDP_DRIVER.is_file():
        print(f"[desktop] cdp_driver.py not found at {_CDP_DRIVER}", file=sys.stderr)
        return 2

    # Bare-Windows baseline: kill any leftover ComfyUI + Python backend,
    # then wipe install + user state. Always-on; mirrors docker's
    # per-container freshness model.
    _kill_existing(desktop_mode)
    _wipe_comfy_state()
    # Seed Manager config + legacy-UI flag right after wipe. Harmless when
    # the install path uses the Manager-UI tile (default); required when it
    # falls back to /customnode/install/git_url (branch-pinned install).
    _write_manager_security_config()
    _enable_manager_legacy_ui()

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

    # Branch resolution: --branch wins, else "dev" when --dev is passed, else "main".
    # Threaded through as NODE_BRANCH so cdp_driver's post-install branch-swap
    # step targets the right ref (git fetch/checkout/pull to <node_branch> HEAD).
    dev = bool(getattr(args, "dev", False))
    node_branch = getattr(args, "branch", None) or ("dev" if dev else "main")

    # Manager installs the node via the in-app GUI flow, then cdp_driver
    # swaps to node_branch. We shallow-clone locally so we can enumerate
    # workflows/*.json from disk (avoids hitting api.github.com/repos/.../
    # contents which the macOS hosted-runner pool's NAT'd egress IPs
    # frequently 403 with anon rate-limit). cdp_driver picks up the list
    # via COMFY_TEST_WORKFLOWS env.
    from comfy_test.cli._nodelink import clone_node, expand_nodelink

    url = expand_nodelink(args.nodelink).rstrip(".git")
    node_name = url.rsplit("/", 1)[-1]

    clone_root = Path(tempfile.mkdtemp(prefix="comfy-test-desktop-clone-"))
    atexit.register(lambda: shutil.rmtree(clone_root, ignore_errors=True))
    workflow_names: list[str] = []
    node_sha: Optional[str] = None
    try:
        clone_node(url, node_branch, clone_root, log_prefix="[desktop]")
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
    print(f"[desktop] node: {node_name}  (URL: {url}, branch: {node_branch}, "
          f"sha: {node_sha[:12] if node_sha else 'unknown'}, "
          f"workflows: {workflow_names})")

    # Logs dir matches the cli/run.py shape: <run_id>/<branch>/<platform>/
    # so dispatch-test.yml's publish step finds results.json with the same
    # `find -path "*/<short>-*/<branch>/<platform>/results.json"` glob it
    # uses for cpu / gpu jobs. --desktop and --desktop --dev share the same
    # platform dir; separation comes naturally from the branch subdir
    # (main/ vs dev/) same as cpu vs gpu.
    logs_dir = resolve_logs_dir_for_sandbox(node_name, node_branch, desktop_mode)
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
    # getattr: run_desktop is also reached from `docker run --desktop_windows`,
    # whose parser doesn't define --refresh-app.
    app_path = _ensure_desktop_app(
        desktop_mode, refresh=getattr(args, "refresh_app", False))
    stdout_log = debug_dir / "electron_stdout.log"
    _launch(app_path, desktop_mode, stdout_log)
    screencap_proc = _start_host_screencap(logs_dir, desktop_mode)
    print(f"[desktop] launched {app_path}, waiting for DevToolsActivePort...")
    try:
        cdp_port = _wait_for_cdp(desktop_mode, 240)
    finally:
        # macOS stops host capture here because cdp_driver then writes
        # higher-indexed frame_*.png that supersede it.
        #
        # Windows deliberately does NOT: CDP capture produces nothing on this
        # host (the Electron window renders black, so captureScreenshot hangs
        # for every page), which makes the OS-level grab the only thing that
        # ever sees anything. It runs for the whole run and is torn down by
        # the atexit hook _start_host_screencap_windows registered.
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
    # cdp_driver relaunches the app itself after Apply-Changes and passes no
    # env= of its own, so it inherits this dict. Without the correction that
    # relaunched instance writes DevToolsActivePort back under the SYSTEM
    # profile and the reconnect hangs.
    env.update(_user_env_overrides())
    env.update({
        "PYTHONUNBUFFERED": "1",
        "COMFY_TEST_CUDA": "1" if desktop_mode == "windows_cuda" else "0",
        "COMFY_TEST_LOGS_DIR": str(logs_dir),
        "COMFY_TEST_DEBUG_DIR": str(debug_dir),
        "NODE_REPO": url.rsplit("github.com/", 1)[-1],
        # Manager installs the CNR nightly (a main-branch snapshot); cdp_driver
        # then runs `git fetch/checkout/pull <NODE_BRANCH>` in the installed
        # node dir so the test targets the exact branch HEAD.
        "NODE_BRANCH": node_branch,
        "NODE_NAME": node_name,
        # Pre-enumerated from the local clone above. cdp_driver's
        # _fetch_workflow_list_from_repo short-circuits on this and skips
        # the api.github.com call (which the macOS hosted-runner pool
        # frequently 403s with anonymous rate-limit).
        "COMFY_TEST_WORKFLOWS": ",".join(workflow_names),
        # cdp_driver writes these into results.json so the dashboard can
        # render the cell colored by pass/fail and match the cpu schema.
        "COMFY_TEST_NODE_SHA": node_sha or "",
        # underscored spelling is the config_key form results.json records
        "COMFY_TEST_DESKTOP_LANE": desktop_lane_id(desktop_mode),
        # cdp_driver's post-Apply-Changes relaunch picks the executable from
        # these. Without them it falls back to the CI-installed path.
        "COMFY_DESKTOP_APP_EXE": str(app_path),
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
        # encoding/errors must be explicit: cdp_driver relays ComfyUI's log
        # file, which is UTF-8, while text=True alone would decode this pipe
        # with the console's cp1252 default and mangle or raise on it.
        proc = subprocess.Popen(
            [str(venv_python), str(_CDP_DRIVER)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
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
    _generate_index(logs_dir, env["NODE_REPO"], desktop_mode, dev=dev)

    _print_workflow_summary(logs_dir / "results.json", tag="desktop")

    # Best-effort: leave the Desktop app open so the user can poke around.
    print(f"[desktop] DONE (rc={rc})")
    print(f"[desktop] open {logs_dir / 'index.html'} to view the report")
    return rc


def _print_workflow_summary(results_json: Path, tag: str = "desktop") -> None:
    """Print a compact pass/fail table from results.json -- one line per
    workflow with a status icon, duration, and (for failures) the first
    line of the error message. Followed by an aggregate `N/M passed`
    line. Called at end-of-run so users see outcomes without opening
    the HTML report."""
    if not results_json.is_file():
        return
    try:
        data = json.loads(results_json.read_text())
    except Exception:
        return
    s = data.get("summary") or {}
    total = int(s.get("total", 0))
    passed = int(s.get("passed", 0))
    failed = int(s.get("failed", 0))
    other = max(0, total - passed - failed)
    icons = {"pass": "+", "fail": "x", "error": "x", "skip": "-"}
    # "platform" is the pre-rename key; already-published results.json files
    # on gh-pages still carry it and are never rewritten.
    _lane = data.get("lane") or data.get("platform") or "?"
    print(f"\n[{tag}] === Workflow summary ({_lane}) ===",
          flush=True)
    for w in data.get("workflows") or []:
        name = w.get("name", "?")
        status = w.get("status", "?")
        icon = icons.get(status, "?")
        dur = f"{w.get('duration_seconds', 0):>4}s"
        err = ""
        if status != "pass":
            first = ((w.get("error") or "").splitlines() or [""])[0].strip()
            if first:
                err = f"  -- {first[:80]}"
        print(f"[{tag}]   {icon} {name:40s} {dur}{err}", flush=True)
    tail = f"{passed}/{total} passed"
    if failed:
        tail += f", {failed} failed"
    if other:
        tail += f", {other} other"
    print(f"[{tag}]   -> {tail}", flush=True)
