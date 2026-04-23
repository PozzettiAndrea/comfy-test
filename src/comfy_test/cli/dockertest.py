"""Dockertest command — clone a node from a git URL and run comfy-test against it
inside an isolated Docker container with GPU passthrough.

Supports both Windows hosts (process-isolated Windows containers) and Linux hosts
(NVIDIA Container Toolkit).

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

DOCKER_IMAGE_WINDOWS = "comfy-test-windows-gpu:full"
DOCKER_IMAGE_LINUX = "comfy-test-linux-gpu:full"
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
    """Locate docker — PATH first, then the Windows default install dir."""
    exe = shutil.which("docker")
    if exe:
        return exe
    if sys.platform == "win32":
        fallback = Path(r"C:\Program Files\Docker\docker.exe")
        return str(fallback) if fallback.exists() else None
    return None


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


def _docker_preflight_windows(docker_exe: str) -> Optional[str]:
    """Check docker is in Windows-container mode and the image exists. Returns error string or None."""
    r = subprocess.run([docker_exe, "info", "--format", "{{.OSType}}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "windows" not in r.stdout.lower():
        return (
            f"Docker is not in Windows-container mode (OSType={r.stdout.strip()!r}). "
            "Run utils/comfy-test/docker/windows-gpu/install-host.ps1 as admin, or switch "
            "Docker Desktop to Windows containers."
        )
    r = subprocess.run([docker_exe, "image", "inspect", DOCKER_IMAGE_WINDOWS],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (
            f"Image {DOCKER_IMAGE_WINDOWS} not found. Build it:\n"
            f"  powershell -ExecutionPolicy Bypass -File "
            r"D:\utils\comfy-test\docker\windows-gpu\build.ps1 -Target full"
        )
    return None


def _docker_preflight_linux(docker_exe: str) -> Optional[str]:
    """Check docker is in Linux mode, NVIDIA runtime is available, and image exists."""
    r = subprocess.run([docker_exe, "info", "--format", "{{.OSType}}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "linux" not in r.stdout.lower():
        return f"Docker is not in Linux mode (OSType={r.stdout.strip()!r})."

    r = subprocess.run([docker_exe, "image", "inspect", DOCKER_IMAGE_LINUX],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (
            f"Image {DOCKER_IMAGE_LINUX} not found. Build it:\n"
            f"  bash utils/comfy-test/docker/linux-gpu/build.sh"
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


def _run_windows(args, docker_exe: str, target_platform: str, gpu: bool,
                 node_path: Path, node_name: str, logs_dir: Path, timestamp: str) -> int:
    """Run dockertest on a Windows host with process-isolated Windows containers."""
    # --persist: bind-mount the full workspace AND the comfy-env cache (C:\ce, where pixi
    # isolation envs live) to the host. Without the C:\ce mount, the broken pixi env dies
    # with --rm and we can't dumpbin /dependents on its DLLs from outside the container.
    # --persist implies --keep-clone so the installed custom_nodes/<node> entry (which may still
    # reference the cloned source for case-sensitive imports) stays valid after the run.
    workspace_dir = None
    env_cache_dir = None
    if args.persist:
        workspace_dir = Path.home() / "comfy-test-workspaces" / f"{node_name}-{timestamp}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        env_cache_dir = Path.home() / "comfy-test-env-cache" / f"{node_name}-{timestamp}"
        env_cache_dir.mkdir(parents=True, exist_ok=True)
        args.keep_clone = True

    # Stage on C:\ if any source is a Dev Drive without both wcifs+bindflt
    stage = _needs_stage(str(node_path)) or _needs_stage(str(logs_dir))
    if workspace_dir is not None:
        stage = stage or _needs_stage(str(workspace_dir))
    if env_cache_dir is not None:
        stage = stage or _needs_stage(str(env_cache_dir))
    workspace_mount_src = None
    env_cache_mount_src = None
    if stage:
        DOCKER_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
        node_mount_src = DOCKER_STAGE_ROOT / "node" / node_name
        logs_mount_src = DOCKER_STAGE_ROOT / "logs"
        logs_mount_src.mkdir(parents=True, exist_ok=True)
        node_mount_src.parent.mkdir(parents=True, exist_ok=True)
        if workspace_dir is not None:
            workspace_mount_src = DOCKER_STAGE_ROOT / "workspace"
            workspace_mount_src.mkdir(parents=True, exist_ok=True)
        if env_cache_dir is not None:
            env_cache_mount_src = DOCKER_STAGE_ROOT / "env_cache"
            env_cache_mount_src.mkdir(parents=True, exist_ok=True)
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
        if workspace_dir is not None:
            workspace_mount_src = workspace_dir
        if env_cache_dir is not None:
            env_cache_mount_src = env_cache_dir

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
    ]
    if workspace_mount_src is not None:
        docker_cmd += ["-v", f"{workspace_mount_src}:C:\\workspaces"]
    if env_cache_mount_src is not None:
        docker_cmd += ["-v", f"{env_cache_mount_src}:C:\\ce"]
    docker_cmd += [
        "-w", container_node_path,
        "-e", f"COMFY_TEST_GPU={'1' if gpu else '0'}",
        DOCKER_IMAGE_WINDOWS,
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

    # Copy staged workspace back to workspace_dir
    if stage and workspace_mount_src is not None and workspace_dir is not None and any(workspace_mount_src.iterdir()):
        subprocess.run(
            ["robocopy", str(workspace_mount_src), str(workspace_dir),
             "/E", "/XJ", "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True,
        )

    # Copy staged env cache back to env_cache_dir
    if stage and env_cache_mount_src is not None and env_cache_dir is not None and any(env_cache_mount_src.iterdir()):
        subprocess.run(
            ["robocopy", str(env_cache_mount_src), str(env_cache_dir),
             "/E", "/XJ", "/R:1", "/W:1",
             "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True,
        )

    if workspace_dir is not None:
        print(f"[dockertest] Workspace: {workspace_dir}")
    if env_cache_dir is not None:
        print(f"[dockertest] Env cache: {env_cache_dir}")

    return result.returncode


def _run_linux(args, docker_exe: str, gpu: bool,
               node_path: Path, node_name: str, logs_dir: Path, timestamp: str) -> int:
    """Run dockertest on a Linux host with NVIDIA Container Toolkit."""
    workspace_dir = None
    if args.persist:
        workspace_dir = Path.home() / "comfy-test-workspaces" / f"{node_name}-{timestamp}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        args.keep_clone = True

    # Build comfy-test args inside the container
    ct_args = ["run", "--platform", "linux"]
    if args.branch:
        ct_args.extend(["--branch", args.branch])
    if gpu:
        ct_args.append("--gpu")
    if args.workflow:
        ct_args.extend(["--workflow", args.workflow])

    container_node_path = f"/node"
    docker_cmd = [
        docker_exe, "run", "--rm",
        "--gpus", "all",
        "-v", f"{node_path}:{container_node_path}",
        "-v", f"{logs_dir}:/logs",
    ]
    if workspace_dir is not None:
        docker_cmd += ["-v", f"{workspace_dir}:/workspaces"]
    docker_cmd += [
        "-w", container_node_path,
        "-e", f"COMFY_TEST_GPU={'1' if gpu else '0'}",
        DOCKER_IMAGE_LINUX,
    ] + ct_args

    print(f"[dockertest] Running: {' '.join(docker_cmd)}")
    result = subprocess.run(docker_cmd)

    if workspace_dir is not None:
        print(f"[dockertest] Workspace: {workspace_dir}")

    return result.returncode


def cmd_dockertest(args) -> int:
    """Clone a node from a URL (or local path) and run comfy-test in Docker."""
    # --portable is a shorthand for --platform windows-portable; reject mixing both.
    if args.portable and args.platform and args.platform != "windows-portable":
        print(f"[dockertest] --portable conflicts with --platform={args.platform}", file=sys.stderr)
        return 1

    # Platform handling
    host_platform = _detect_host_platform()
    if args.portable:
        target_platform = "windows-portable"
    else:
        target_platform = args.platform or host_platform

    # Validate host/target combination
    if host_platform == "linux":
        if target_platform != "linux":
            print(f"[dockertest] Linux host can only target 'linux', got '{target_platform}'.",
                  file=sys.stderr)
            return 1
        if args.portable:
            print("[dockertest] --portable is not supported on Linux.", file=sys.stderr)
            return 1
    elif host_platform == "windows":
        if target_platform not in ("windows", "windows-portable"):
            print(f"[dockertest] Windows host only supports 'windows'/'windows-portable', "
                  f"got '{target_platform}'.", file=sys.stderr)
            return 1
    else:
        print(f"[dockertest] Docker mode is not supported on {host_platform}.", file=sys.stderr)
        return 1

    gpu = bool(args.gpu)

    # Docker preflight
    docker_exe = _find_docker()
    if not docker_exe:
        hint = "Run install-host.ps1 first." if host_platform == "windows" else "Install docker."
        print(f"[dockertest] docker not found. {hint}", file=sys.stderr)
        return 1

    if host_platform == "windows":
        err = _docker_preflight_windows(docker_exe)
    else:
        err = _docker_preflight_linux(docker_exe)
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

    # Logs dir — user-provided or a persistent per-run dir under ~/comfy-test-logs/.
    # NOT work_root/logs: work_root is rmtree'd at the end, which would delete the logs.
    timestamp = datetime.now().strftime("%H%M")
    if args.logs_dir:
        logs_dir = Path(args.logs_dir).resolve()
    else:
        logs_dir = Path.home() / "comfy-test-logs" / f"{node_name}-{timestamp}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Dispatch to platform-specific runner
    if host_platform == "windows":
        rc = _run_windows(args, docker_exe, target_platform, gpu,
                          node_path, node_name, logs_dir, timestamp)
    else:
        rc = _run_linux(args, docker_exe, gpu,
                        node_path, node_name, logs_dir, timestamp)

    # Clean temp clone dir. logs_dir lives outside work_root now, so the rmtree is safe.
    if not args.keep_clone:
        shutil.rmtree(work_root, ignore_errors=True)
    print(f"[dockertest] Logs: {logs_dir}")

    return rc


def add_dockertest_parser(subparsers):
    """Register the `dockertest` subcommand."""
    p = subparsers.add_parser(
        "dockertest",
        help="Clone a node from URL and run comfy-test in an isolated Docker container",
    )
    p.add_argument("nodelink", help="Git URL (or local path) to the custom node")
    p.add_argument("--branch", "-b", default=None, help="Git branch to clone (default: repo default)")
    p.add_argument("--gpu", action="store_true",
                   help="Enable GPU mode (CUDA passthrough). Default: CPU only.")
    p.add_argument("--platform", default=None,
                   help="comfy-test target platform (default: match host)")
    p.add_argument("--portable", action="store_true",
                   help="Test against portable ComfyUI (shorthand for --platform windows-portable)")
    p.add_argument("--workflow", default=None, help="Run only this specific workflow")
    p.add_argument("--logs-dir", default=None,
                   help="Host directory for logs (default: a temp dir alongside the clone)")
    p.add_argument("--keep-clone", action="store_true",
                   help="Don't delete the cloned node after the run")
    p.add_argument("--persist", action="store_true",
                   help="Bind-mount the workspace to the host so ComfyUI install, "
                        ".venv, and pixi envs survive after the container exits. "
                        "Saved to ~/comfy-test-workspaces/<node>-<HHMM>/. Implies --keep-clone.")
    p.set_defaults(func=cmd_dockertest)
