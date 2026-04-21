"""Dockertest command — clone a node from a git URL and run comfy-test against it
inside an isolated Windows container with GPU passthrough.

Standalone: no cds, no CDS_ROOT, no YAML config. Everything is driven by the
nodelink URL + optional flags.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

DOCKER_IMAGE = "comfy-test-windows-gpu:full"
DOCKER_GPU_DEVICE = "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599"  # GUID_DEVINTERFACE_DISPLAY_ADAPTER
DOCKER_STAGE_ROOT = Path(r"C:\cds-docker-stage")


def _detect_host_platform() -> str:
    """Return the comfy-test platform name for the current host."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def _find_docker() -> Optional[str]:
    """Locate docker.exe — PATH first, then the default install dir."""
    exe = shutil.which("docker")
    if exe:
        return exe
    fallback = Path(r"C:\Program Files\Docker\docker.exe")
    return str(fallback) if fallback.exists() else None


def _needs_stage(vol_path: str) -> bool:
    """True if the volume holding vol_path is a Dev Drive missing wcifs or bindflt attached.

    Docker bind mounts on Win11 Dev Drives require both filters; if either is missing,
    we stage to a non-Dev-Drive path.
    """
    if sys.platform != "win32":
        return False
    drive = Path(vol_path).drive
    if not drive:
        return False
    q = subprocess.run(["fsutil", "devdrv", "query", drive + "\\"],
                       capture_output=True, text=True)
    if q.returncode != 0:
        return False
    out = q.stdout.lower()
    if "not a developer volume" in out or "trusted developer volume" not in out:
        return False
    attached = out.split("filters currently attached")[-1] if "filters currently attached" in out else ""
    return not ("wcifs" in attached and "bindflt" in attached)


def _docker_preflight(docker_exe: str) -> Optional[str]:
    """Check docker is in Windows-container mode and the image exists. Returns error string or None."""
    r = subprocess.run([docker_exe, "info", "--format", "{{.OSType}}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "windows" not in r.stdout.lower():
        return (
            f"Docker is not in Windows-container mode (OSType={r.stdout.strip()!r}). "
            "Run utils/comfy-test/docker/windows-gpu/install-host.ps1 as admin, or switch "
            "Docker Desktop to Windows containers."
        )
    r = subprocess.run([docker_exe, "image", "inspect", DOCKER_IMAGE],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (
            f"Image {DOCKER_IMAGE} not found. Build it:\n"
            f"  powershell -ExecutionPolicy Bypass -File "
            r"D:\utils\comfy-test\docker\windows-gpu\build.ps1 -Target full"
        )
    return None


def _clone_node(nodelink: str, branch: Optional[str], dest: Path) -> str:
    """Clone nodelink (git URL) into dest. Returns the node folder name.

    If nodelink is a local path, copy from there instead.
    """
    src_path = Path(nodelink) if Path(nodelink).exists() else None
    if src_path and src_path.is_dir():
        node_name = src_path.name
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / node_name
        if target.exists():
            shutil.rmtree(target)
        print(f"[dockertest] LOCAL PATH → copying {src_path} to {target}")
        shutil.copytree(src_path, target, symlinks=False,
                        ignore=shutil.ignore_patterns(".venv", "venv", ".git",
                                                      "__pycache__", ".comfy-test"))
        return node_name
    # Treat as git URL; derive node name from URL
    node_name = nodelink.rstrip("/").split("/")[-1].removesuffix(".git")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / node_name
    if target.exists():
        shutil.rmtree(target)
    branch_desc = f"branch={branch}" if branch else "default branch"
    print(f"[dockertest] URL CLONE → git clone {nodelink} ({branch_desc}) → {target}")
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([nodelink, str(target)])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{r.stderr}")
    # Surface the resolved commit so user can verify what was pulled
    sha = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    if sha.returncode == 0:
        short = sha.stdout.strip()[:12]
        msg = subprocess.run(["git", "-C", str(target), "log", "-1", "--format=%s (%ci)"],
                             capture_output=True, text=True)
        subj = msg.stdout.strip() if msg.returncode == 0 else ""
        print(f"[dockertest] cloned {node_name} @ {short}  {subj}")
    return node_name


def cmd_dockertest(args) -> int:
    """Clone a node from a URL (or local path) and run comfy-test in Docker."""
    # Platform handling
    host_platform = _detect_host_platform()
    target_platform = args.platform or host_platform
    if host_platform != "windows":
        print(f"[dockertest] Host is {host_platform}; Docker path currently only supports "
              f"Windows hosts.", file=sys.stderr)
        return 1
    if target_platform not in ("windows", "windows-portable"):
        print(f"[dockertest] --platform={target_platform} not supported by the Windows-GPU image.",
              file=sys.stderr)
        return 1

    gpu = bool(args.gpu)

    # Docker preflight
    docker_exe = _find_docker()
    if not docker_exe:
        print("[dockertest] docker not found. Run install-host.ps1 first.", file=sys.stderr)
        return 1
    err = _docker_preflight(docker_exe)
    if err:
        print(f"[dockertest] {err}", file=sys.stderr)
        return 1

    # Clone or copy node
    work_root = Path(tempfile.mkdtemp(prefix="comfy-test-dockertest-"))
    print(f"[dockertest] Working dir: {work_root}")
    try:
        node_name = _clone_node(args.nodelink, args.branch, work_root)
    except Exception as e:
        print(f"[dockertest] {e}", file=sys.stderr)
        shutil.rmtree(work_root, ignore_errors=True)
        return 1
    node_path = work_root / node_name

    # Logs dir — user-provided or a persistent per-run dir under ~\comfy-test-logs\.
    # NOT work_root/logs: work_root is rmtree'd at the end, which would delete the logs.
    if args.logs_dir:
        logs_dir = Path(args.logs_dir).resolve()
    else:
        timestamp = datetime.now().strftime("%H%M")
        logs_dir = Path.home() / "comfy-test-logs" / f"{node_name}-{timestamp}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Stage on C:\ if either source is a Dev Drive without both wcifs+bindflt
    stage = _needs_stage(str(node_path)) or _needs_stage(str(logs_dir))
    if stage:
        DOCKER_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
        node_mount_src = DOCKER_STAGE_ROOT / "node" / node_name
        logs_mount_src = DOCKER_STAGE_ROOT / "logs"
        logs_mount_src.mkdir(parents=True, exist_ok=True)
        node_mount_src.parent.mkdir(parents=True, exist_ok=True)
        print(f"[dockertest] Dev Drive filters not attached; staging to {DOCKER_STAGE_ROOT}")
        rc = subprocess.run(
            ["robocopy", str(node_path), str(node_mount_src),
             "/MIR", "/XJ", "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
             "/XD", ".venv", "venv", ".git", "__pycache__", ".pytest_cache",
             ".comfy-test", "node_modules"],
            capture_output=True, text=True,
        )
        if rc.returncode >= 8:
            print(f"[dockertest] robocopy failed (exit {rc.returncode})\n{rc.stdout}\n{rc.stderr}",
                  file=sys.stderr)
            return 1
    else:
        node_mount_src = node_path
        logs_mount_src = logs_dir

    # Build comfy-test args inside the container
    ct_args = ["run", "--platform", target_platform]
    if args.branch:
        ct_args.extend(["--branch", args.branch])
    if gpu:
        ct_args.append("--gpu")
    if args.workflow:
        ct_args.extend(["--workflow", args.workflow])

    container_node_path = f"C:\\{node_name}"
    docker_cmd = [
        docker_exe, "run", "--rm",
        "--isolation=process",
        "--device", DOCKER_GPU_DEVICE,
        "-v", f"{node_mount_src}:{container_node_path}",
        "-v", f"{logs_mount_src}:C:\\logs",
        "-w", container_node_path,
        "-e", f"COMFY_TEST_GPU={'1' if gpu else '0'}",
        DOCKER_IMAGE,
    ] + ct_args

    print(f"[dockertest] Running: {' '.join(docker_cmd)}")
    result = subprocess.run(docker_cmd)

    # Copy staged logs back to logs_dir
    if stage and any(logs_mount_src.iterdir()):
        subprocess.run(
            ["robocopy", str(logs_mount_src), str(logs_dir),
             "/E", "/XJ", "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True,
        )

    # Clean temp clone dir. logs_dir lives outside work_root now, so the rmtree is safe.
    if not args.keep_clone:
        shutil.rmtree(work_root, ignore_errors=True)
    print(f"[dockertest] Logs: {logs_dir}")

    return result.returncode


def add_dockertest_parser(subparsers):
    """Register the `dockertest` subcommand."""
    p = subparsers.add_parser(
        "dockertest",
        help="Clone a node from URL and run comfy-test in an isolated Windows container",
    )
    p.add_argument("nodelink", help="Git URL (or local path) to the custom node")
    p.add_argument("--branch", "-b", default=None, help="Git branch to clone (default: repo default)")
    p.add_argument("--gpu", action="store_true",
                   help="Enable GPU mode (CUDA passthrough). Default: CPU only.")
    p.add_argument("--platform", default=None,
                   help="comfy-test target platform (default: match host)")
    p.add_argument("--workflow", default=None, help="Run only this specific workflow")
    p.add_argument("--logs-dir", default=None,
                   help="Host directory for logs (default: a temp dir alongside the clone)")
    p.add_argument("--keep-clone", action="store_true",
                   help="Don't delete the cloned node after the run")
    p.set_defaults(func=cmd_dockertest)
