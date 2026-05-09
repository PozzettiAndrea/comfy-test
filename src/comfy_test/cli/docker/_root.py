"""Pick a single root directory for all `comfy-test docker` host artifacts.

Layout (under <root>):
    <root>/logs        -- `docker test` results (default --logs-dir)
    <root>/stage       -- robocopy staging fallback (Windows-only)
    <root>/installers  -- auto-downloaded driver/git installers cache (Windows)
    <root>/workspaces  -- --persist ComfyUI workspace mount
    <root>/env-cache   -- --persist pixi env cache
    <root>/artifacts   -- `docker build --save` .tar.zst output

Selection priority:
    1. $COMFY_TEST_DOCKER_ROOT (explicit override)
    2. Windows: first Trusted Developer Volume with >= MIN_FREE_GB free,
       via `fsutil devdrv enum`. Returns <drive>:\\docker.
    3. Windows fallback: C:\\docker.
    4. Non-Windows: ~/.comfy-test/docker.

Per-component env vars (COMFY_TEST_LOGS_DIR, etc.) still override the
auto-derived defaults -- callers should consult those first.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


MIN_FREE_GB = 50

_root_cache: Optional[Path] = None
_root_source_cache: Optional[str] = None  # "env" | "devdrv" | "fallback" -- for diagnostics


def _is_trusted_dev_drive(drive: str) -> bool:
    """`fsutil devdrv query <drive>` returns 'This is a trusted developer volume.'
    when the volume is one. Cheap probe -- single subprocess call per letter."""
    try:
        r = subprocess.run(
            ["fsutil", "devdrv", "query", drive],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    return "trusted developer volume" in r.stdout.lower()


def _enum_dev_drives_windows() -> list:
    """Return list of (drive_letter_with_slash, free_gb) for Trusted Developer Volumes.

    Windows has no first-class enumeration subcommand (no `fsutil devdrv enum`),
    so we walk drive letters that exist and probe each via `fsutil devdrv query`.
    """
    if sys.platform != "win32":
        return []
    drives = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":  # skip A,B,C -- A/B legacy floppy, C never a dev drive
        drive = f"{letter}:\\"
        try:
            if not Path(drive).exists():
                continue
            usage = shutil.disk_usage(drive)
        except OSError:
            continue
        if not _is_trusted_dev_drive(drive):
            continue
        free_gb = usage.free / (1024 ** 3)
        drives.append((drive, free_gb))
    return drives


def _pick_dev_drive() -> Optional[Path]:
    """Return <drive>:\\ for the Trusted Dev Drive with the most free space >= MIN_FREE_GB."""
    drives = [(d, f) for d, f in _enum_dev_drives_windows() if f >= MIN_FREE_GB]
    if not drives:
        return None
    drives.sort(key=lambda x: x[1], reverse=True)
    return Path(drives[0][0])


def get_docker_root() -> Path:
    """Resolved + cached root path. Created with mkdir on first call."""
    global _root_cache, _root_source_cache
    if _root_cache is not None:
        return _root_cache

    env = os.environ.get("COMFY_TEST_DOCKER_ROOT", "").strip()
    if env:
        root = Path(env)
        source = "env"
    elif sys.platform == "win32":
        dd = _pick_dev_drive()
        if dd is not None:
            root = dd / "docker"
            source = "devdrv"
        else:
            root = Path(r"C:\docker")
            source = "fallback"
    else:
        root = Path.home() / ".comfy-test" / "docker"
        source = "fallback"

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[docker root] could not create {root}: {e}", file=sys.stderr)
        # don't crash callers -- return the path anyway, they can decide
    _root_cache = root
    _root_source_cache = source
    return root


def get_docker_root_with_source() -> Tuple[Path, str]:
    """For diagnostics / `comfy-test docker` list view."""
    root = get_docker_root()
    return root, _root_source_cache or "fallback"


def subdir(name: str) -> Path:
    """`<root>/<name>`, mkdir'd."""
    p = get_docker_root() / name
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p
