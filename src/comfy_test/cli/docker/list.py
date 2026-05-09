"""`comfy-test docker` (with no subcommand) and `comfy-test docker list` —
print known image tags, whether they're loaded locally, and where they live.

Layout:
    Local Docker images
        <tag>   <size>   <created>   <source>
    SMB-served artifacts
        <path>  <size>   <mtime>
    Configured paths
        <env-var>  =  <value>
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


KNOWN_IMAGES = [
    "comfy-test-linux-gpu:full",
    "comfy-test-windows-gpu:full",
]


def _find_docker() -> Optional[str]:
    exe = shutil.which("docker")
    if exe:
        return exe
    if sys.platform == "win32":
        default = r"C:\Program Files\Docker\docker.exe"
        if Path(default).is_file():
            return default
    return None


def _human_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:6.1f} {unit}"
        n /= 1024
    return f"{n:6.1f} PB"


def _rel_time(iso: str) -> str:
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            tz_idx = max(tail.find("+"), tail.find("-"))
            if tz_idx > 0:
                s = head + tail[tz_idx:]
            else:
                s = head + "+00:00"
        t = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - t
        sec = int(delta.total_seconds())
        if sec < 60:    return f"{sec}s ago"
        if sec < 3600:  return f"{sec // 60}m ago"
        if sec < 86400: return f"{sec // 3600}h ago"
        return f"{sec // 86400}d ago"
    except (ValueError, TypeError):
        return iso[:19]


def _local_image_info(docker_exe: str, tag: str) -> Optional[dict]:
    r = subprocess.run(
        [docker_exe, "image", "inspect", tag],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        return data[0] if data else None
    except (json.JSONDecodeError, IndexError):
        return None


def _smb_artifact_paths() -> list:
    """Probable .tar.zst artifact locations: $COMFY_TEST_DOCKER_ARTIFACT_PATH +
    a sibling guess for the other OS."""
    artifact = os.environ.get("COMFY_TEST_DOCKER_ARTIFACT_PATH", "")
    paths = []
    if artifact:
        p = Path(artifact)
        paths.append(p)
        if "windows-gpu" in p.name:
            paths.append(p.with_name(p.name.replace("windows-gpu", "linux-gpu")))
        elif "linux-gpu" in p.name:
            paths.append(p.with_name(p.name.replace("linux-gpu", "windows-gpu")))
    return paths


def cmd_docker_list(args=None) -> int:
    docker_exe = _find_docker()

    print("Local Docker images")
    print("-" * 70)
    if not docker_exe:
        print("  docker not found on PATH")
    else:
        for tag in KNOWN_IMAGES:
            info = _local_image_info(docker_exe, tag)
            if info is None:
                print(f"  {tag:34s}  (not loaded)")
                continue
            size = _human_size(info.get("Size", 0))
            created = _rel_time(info.get("Created", ""))
            print(f"  {tag:34s}  {size}  created {created}")

    print()
    print("SMB-served artifacts ($COMFY_TEST_DOCKER_ARTIFACT_PATH + sibling)")
    print("-" * 70)
    paths = _smb_artifact_paths()
    if not paths:
        print("  COMFY_TEST_DOCKER_ARTIFACT_PATH not set;")
        print("  configure via `comfy-test settings` -> Paths tab.")
    else:
        for p in paths:
            try:
                if p.is_file():
                    size = _human_size(p.stat().st_size)
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
                    print(f"  {str(p):55s}  {size}  modified {mtime}")
                else:
                    print(f"  {str(p):55s}  (not present)")
            except (OSError, PermissionError) as e:
                print(f"  {str(p):55s}  (unreachable: {type(e).__name__})")

    print()
    print("Configured paths")
    print("-" * 70)
    for var in ("COMFY_TEST_DOCKER_STAGE_DIR",
                "COMFY_TEST_INSTALLERS_DIR",
                "COMFY_TEST_DOCKER_ARTIFACT_PATH"):
        val = os.environ.get(var, "(unset)")
        print(f"  {var} = {val}")

    return 0


def add_docker_list_parser(subparsers):
    """Register `docker list`."""
    p = subparsers.add_parser(
        "list",
        help="Show known images, whether they're loaded locally, and SMB artifacts",
    )
    p.set_defaults(func=cmd_docker_list)
