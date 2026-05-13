"""`comfy-test docker test` -- clone a node from a git URL and run comfy-test
against it inside an isolated Docker container with GPU passthrough.

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

from . import _root


def _detect_host_platform() -> str:
    """Return the comfy-test platform name for the current host."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def _find_docker() -> Optional[str]:
    """Locate docker -- PATH first, then the Windows default install dir."""
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


def _expand_nodelink(nodelink: str) -> str:
    """Expand owner/repo shorthand to a full GitHub URL."""
    # Skip if it's a local path, already a URL, or doesn't match owner/repo pattern
    if Path(nodelink).exists() or "://" in nodelink or nodelink.count("/") != 1:
        return nodelink
    owner, repo = nodelink.split("/", 1)
    if not owner or not repo:
        return nodelink
    url = f"https://github.com/{owner}/{repo}.git"
    print(f"[docker run] Expanding {nodelink} -> {url}")
    return url


def _is_url_nodelink(nodelink: str) -> bool:
    """True if nodelink is a remote URL (or owner/repo shorthand), not a local dir.

    URL-mode skips host cloning entirely -- the container clones inside its
    writable layer via the entrypoint. Only local paths bind-mount their
    contents into the container.
    """
    expanded = _expand_nodelink(nodelink)
    p = Path(expanded)
    return not (p.exists() and p.is_dir())


def _node_name_from_url(nodelink: str) -> str:
    """Derive the node directory name from a URL (or owner/repo shorthand)."""
    expanded = _expand_nodelink(nodelink)
    return expanded.rstrip("/").split("/")[-1].removesuffix(".git")


def _copy_local_node(nodelink: str, dest: Path) -> str:
    """Copy a local node directory into dest. Returns the node folder name.

    URL handling lives in `_is_url_nodelink` + the container's entrypoint --
    this function intentionally only handles the local-path case.
    """
    nodelink = _expand_nodelink(nodelink)
    src_path = Path(nodelink)
    if not src_path.exists():
        raise RuntimeError(f"Local path not found: {nodelink}")
    if not src_path.is_dir():
        raise RuntimeError(f"Local path is not a directory: {nodelink}")
    node_name = src_path.name
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / node_name
    if target.exists():
        shutil.rmtree(target)
    print(f"[docker run] LOCAL PATH -> copying {src_path} to {target}")
    shutil.copytree(src_path, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".venv", "venv", ".git",
                                                  "__pycache__", ".comfy-test"))
    return node_name


def _run_windows(args, docker_exe: str, target_platform: str, gpu: bool,
                 node_path: Optional[Path], node_name: str, logs_dir: Path,
                 timestamp: str) -> int:
    """Run docker test on a Windows host with process-isolated Windows containers.

    URL mode (`node_path is None`): no node bind-mount; container clones from
    `args.nodelink` via the entrypoint. Local mode: bind-mount node_path.
    """
    url_mode = node_path is None

    # --persist: bind-mount the full workspace AND the comfy-env cache (C:\ce, where pixi
    # isolation envs live) to the host. Without the C:\ce mount, the broken pixi env dies
    # with --rm and we can't dumpbin /dependents on its DLLs from outside the container.
    # --persist implies --keep-clone so the installed custom_nodes/<node> entry (which may still
    # reference the cloned source for case-sensitive imports) stays valid after the run.
    workspace_dir = None
    env_cache_dir = None
    if args.persist:
        workspace_dir = _root.subdir("workspaces") / f"{node_name}-{timestamp}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        env_cache_dir = _root.subdir("env-cache") / f"{node_name}-{timestamp}"
        env_cache_dir.mkdir(parents=True, exist_ok=True)
        args.keep_clone = True

    # Stage on C:\ if any source is a Dev Drive without both wcifs+bindflt.
    # URL mode has no node_path to stage; only logs (and persist dirs) matter.
    stage = _needs_stage(str(logs_dir))
    if not url_mode:
        stage = stage or _needs_stage(str(node_path))
    if workspace_dir is not None:
        stage = stage or _needs_stage(str(workspace_dir))
    if env_cache_dir is not None:
        stage = stage or _needs_stage(str(env_cache_dir))
    workspace_mount_src = None
    env_cache_mount_src = None
    node_mount_src = None
    if stage:
        if os.environ.get("COMFY_TEST_DOCKER_STAGE_DIR"):
            stage_root = Path(os.environ["COMFY_TEST_DOCKER_STAGE_DIR"])
            stage_root.mkdir(parents=True, exist_ok=True)
        else:
            stage_root = _root.subdir("stage")
        logs_mount_src = stage_root / "logs"
        shutil.rmtree(logs_mount_src, ignore_errors=True)
        logs_mount_src.mkdir(parents=True, exist_ok=True)
        if not url_mode:
            node_mount_src = stage_root / "node" / node_name
            shutil.rmtree(node_mount_src, ignore_errors=True)
            node_mount_src.parent.mkdir(parents=True, exist_ok=True)
        if workspace_dir is not None:
            workspace_mount_src = stage_root / "workspace"
            shutil.rmtree(workspace_mount_src, ignore_errors=True)
            workspace_mount_src.mkdir(parents=True, exist_ok=True)
        if env_cache_dir is not None:
            env_cache_mount_src = stage_root / "env_cache"
            # NOT wiped -- env cache is the whole point of caching across runs.
            env_cache_mount_src.mkdir(parents=True, exist_ok=True)
        print(f"[docker run] Dev Drive filters not attached; staging to {stage_root}")
        if not url_mode:
            rc = subprocess.run(
                ["robocopy", str(node_path), str(node_mount_src),
                 "/MIR", "/XJ", "/R:1", "/W:1",
                 "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                 "/XD", ".venv", "venv", "__pycache__", ".pytest_cache",
                 ".comfy-test", "node_modules"],
                capture_output=True, text=True,
            )
            if rc.returncode >= 8:
                print(f"[docker run] robocopy failed (exit {rc.returncode})\n{rc.stdout}\n{rc.stderr}",
                      file=sys.stderr)
                return 1
    else:
        logs_mount_src = logs_dir
        if not url_mode:
            node_mount_src = node_path
        if workspace_dir is not None:
            workspace_mount_src = workspace_dir
        if env_cache_dir is not None:
            env_cache_mount_src = env_cache_dir

    # Grant the host's `Users` group Modify on every bind-mount source dir.
    # The Windows GPU container now runs as `ContainerUser` (non-admin) — see
    # docker/windows-gpu/Dockerfile. Windows containers project the host's
    # NTFS ACLs through bind mounts; ContainerUser inside maps to the host's
    # `Users` / `Authenticated Users` SID. Without an explicit grant, dirs
    # created with default creator-only ACLs cause `[WinError 5] Access is
    # denied` from inside the container at the first write attempt.
    for _src in (logs_mount_src, node_mount_src, workspace_mount_src, env_cache_mount_src):
        if _src is None:
            continue
        subprocess.run(
            ["icacls", str(_src), "/grant", "Users:(OI)(CI)F", "/T", "/Q"],
            capture_output=True, text=True,
        )

    # Build comfy-test args inside the container
    # Inner `comfy-test run` derives platform from the container's host OS.
    # We pass --portable / --gpu / --workflow / --branch through.
    ct_args = ["run"]
    if args.branch:
        ct_args.extend(["--branch", args.branch])
    if gpu:
        ct_args.append("--gpu")
    if args.portable:
        ct_args.append("--portable")
    if args.workflow:
        ct_args.extend(["--workflow", args.workflow])

    container_node_path = f"C:\\{node_name}"
    docker_cmd = [
        docker_exe, "run", "--rm",
        "--isolation=process",
        "--device", DOCKER_GPU_DEVICE,
        "-v", f"{logs_mount_src}:C:\\logs",
    ]
    if not url_mode:
        # Local mode: bind-mount the copied source dir + set workdir to it.
        docker_cmd += ["-v", f"{node_mount_src}:{container_node_path}"]
    if workspace_mount_src is not None:
        docker_cmd += ["-v", f"{workspace_mount_src}:C:\\workspaces"]
    if env_cache_mount_src is not None:
        docker_cmd += ["-v", f"{env_cache_mount_src}:C:\\ce"]
    if not url_mode:
        docker_cmd += ["-w", container_node_path]
    else:
        # URL mode: entrypoint clones to C:\<node_name> and cd's into it.
        docker_cmd += [
            "-e", f"COMFY_TEST_NODE_URL={_expand_nodelink(args.nodelink)}",
            "-e", f"COMFY_TEST_NODE_NAME={node_name}",
        ]
        if args.branch:
            docker_cmd += ["-e", f"COMFY_TEST_NODE_BRANCH={args.branch}"]
    docker_cmd += [
        "-e", f"COMFY_TEST_GPU={'1' if gpu else '0'}",
    ]
    # Propagate Python pin (if set on host -- usually picked by the YAML dispatcher).
    py_pin = os.environ.get("COMFY_TEST_PYTHON_VERSION", "").strip()
    if py_pin:
        docker_cmd += ["-e", f"COMFY_TEST_PYTHON_VERSION={py_pin}"]
    # Propagate the GHA run URL so results.json's `run_url` field gets a real
    # value (the dashboard's Goto-mode reads it to deep-link cells back to the
    # run that produced them). Without this, the field stamps as null.
    run_url = os.environ.get("COMFY_TEST_RUN_URL", "").strip()
    if run_url:
        docker_cmd += ["-e", f"COMFY_TEST_RUN_URL={run_url}"]
    docker_cmd += [DOCKER_IMAGE_WINDOWS] + ct_args

    print(f"[docker run] Running: {' '.join(docker_cmd)}")
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
        print(f"[docker run] Workspace: {workspace_dir}")
    if env_cache_dir is not None:
        print(f"[docker run] Env cache: {env_cache_dir}")

    return result.returncode


def _run_linux(args, docker_exe: str, gpu: bool,
               node_path: Optional[Path], node_name: str, logs_dir: Path, timestamp: str) -> int:
    """Run docker test on a Linux host with NVIDIA Container Toolkit.

    URL mode (`node_path is None`): no node bind-mount; container clones via
    the entrypoint. Local mode: bind-mount node_path.
    """
    url_mode = node_path is None

    workspace_dir = None
    if args.persist:
        workspace_dir = _root.subdir("workspaces") / f"{node_name}-{timestamp}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        args.keep_clone = True

    # Build comfy-test args inside the container
    # Inner `comfy-test run` derives platform from the container (linux). We
    # pass --gpu / --workflow / --branch through.
    ct_args = ["run"]
    if args.branch:
        ct_args.extend(["--branch", args.branch])
    if gpu:
        ct_args.append("--gpu")
    if args.workflow:
        ct_args.extend(["--workflow", args.workflow])

    container_node_path = f"/node/{node_name}"
    docker_cmd = [
        docker_exe, "run", "--rm",
        "--gpus", "all",
        "--shm-size=8g",
        "-v", f"{logs_dir}:/logs",
    ]
    if not url_mode:
        docker_cmd += ["-v", f"{node_path}:{container_node_path}"]
    if workspace_dir is not None:
        docker_cmd += ["-v", f"{workspace_dir}:/workspaces"]
    if not url_mode:
        docker_cmd += ["-w", container_node_path]
    else:
        docker_cmd += [
            "-e", f"COMFY_TEST_NODE_URL={_expand_nodelink(args.nodelink)}",
            "-e", f"COMFY_TEST_NODE_NAME={node_name}",
        ]
        if args.branch:
            docker_cmd += ["-e", f"COMFY_TEST_NODE_BRANCH={args.branch}"]
    docker_cmd += [
        "-e", f"COMFY_TEST_GPU={'1' if gpu else '0'}",
    ]
    py_pin = os.environ.get("COMFY_TEST_PYTHON_VERSION", "").strip()
    if py_pin:
        docker_cmd += ["-e", f"COMFY_TEST_PYTHON_VERSION={py_pin}"]
    run_url = os.environ.get("COMFY_TEST_RUN_URL", "").strip()
    if run_url:
        docker_cmd += ["-e", f"COMFY_TEST_RUN_URL={run_url}"]
    docker_cmd += [DOCKER_IMAGE_LINUX] + ct_args

    print(f"[docker run] Running: {' '.join(docker_cmd)}")
    result = subprocess.run(docker_cmd)

    if workspace_dir is not None:
        print(f"[docker run] Workspace: {workspace_dir}")

    return result.returncode


def _patch_null_commit_hash(node_path: Path, logs_dir: Path) -> None:
    """Set commit_hash on any results.json the container left as null."""
    sha_proc = subprocess.run(
        ["git", "-C", str(node_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if sha_proc.returncode != 0:
        return
    sha = sha_proc.stdout.strip()
    if not sha:
        return
    import json as _json
    for results_file in logs_dir.rglob("results.json"):
        try:
            data = _json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("commit_hash"):
            continue
        data["commit_hash"] = sha
        results_file.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        print(f"[docker run] Patched commit_hash={sha[:12]} in {results_file}")


def cmd_docker_run(args) -> int:
    """Clone a node from a URL (or local path) and run comfy-test in Docker.

    With --desktop_mac / --desktop_windows / --desktop_windows_gpu, bypass
    the Docker path entirely and drive ComfyUI Desktop on the local host
    via cdp_driver.py. That mode mirrors what the
    `_test-{macos,windows}-desktop.yml` workflows do on a GHA runner --
    used to iterate on cdp_driver behavior without round-tripping CI.
    """
    if sys.platform == "win32":
        from . import _defender
        _defender.warn_if_needed(args)
    desktop_mode = getattr(args, "desktop_mode", None)
    if desktop_mode:
        # Desktop modes don't use docker. --portable has no meaning here.
        if getattr(args, "portable", None):
            print(f"[docker run] --portable conflicts with --desktop_{desktop_mode}",
                  file=sys.stderr)
            return 1
        from comfy_test.cli._desktop_runner import run_desktop  # local: keep optional dep cost low
        return run_desktop(args, desktop_mode)

    # Platform is always derived from the host OS -- no cross-platform tests.
    host_platform = _detect_host_platform()
    if host_platform == "macos":
        print("[docker run] Docker mode is not supported on macOS -- use `comfy-test run` "
              "for native macOS testing.", file=sys.stderr)
        return 1
    if args.portable and host_platform != "windows":
        print("[docker run] --portable is only valid on Windows.", file=sys.stderr)
        return 1
    target_platform = "windows-portable" if args.portable else host_platform

    gpu = bool(args.gpu)

    # Docker preflight
    docker_exe = _find_docker()
    if not docker_exe:
        hint = "Run install-host.ps1 first." if host_platform == "windows" else "Install docker."
        print(f"[docker run] docker not found. {hint}", file=sys.stderr)
        return 1

    if host_platform == "windows":
        err = _docker_preflight_windows(docker_exe)
    else:
        err = _docker_preflight_linux(docker_exe)
    if err:
        print(f"[docker run] {err}", file=sys.stderr)
        return 1

    # URL inputs are cloned by the container itself (no host work_root, no
    # bind-mount of source). Local paths are copied to a host work_root and
    # bind-mounted in.
    nodelink_expanded = _expand_nodelink(args.nodelink)
    if _is_url_nodelink(args.nodelink):
        work_root = None
        node_name = _node_name_from_url(args.nodelink)
        node_path = None  # signals URL mode to _run_{windows,linux}
        branch_desc = f"branch={args.branch}" if args.branch else "default branch"
        print(f"[docker run] URL mode: container will clone {nodelink_expanded} ({branch_desc})")
    else:
        work_root = Path(tempfile.mkdtemp(prefix="comfy-test-dockertest-"))
        print(f"[docker run] Working dir: {work_root}")
        try:
            node_name = _copy_local_node(args.nodelink, work_root)
        except Exception as e:
            print(f"[docker run] {e}", file=sys.stderr)
            shutil.rmtree(work_root, ignore_errors=True)
            return 1
        node_path = work_root / node_name

    # Logs dir -- base directory bind-mounted into the container as /logs (or C:\logs).
    # The container's `comfy-test run` creates its own <short_name>-<timestamp>/<branch>/<platform>
    # subtree under it (see run.py). Don't pre-create a per-run dir here, or the host ends up
    # with doubled levels like ~/comfy-test-logs/ComfyUI-Foo-HHMM/Foo-HHMM/...
    # NOT work_root/logs: work_root is rmtree'd at the end, which would delete the logs.
    timestamp = datetime.now().strftime("%H%M")
    if args.logs_dir:
        logs_dir = Path(args.logs_dir).resolve()
    elif os.environ.get("COMFY_TEST_LOGS_DIR"):
        logs_dir = Path(os.environ["COMFY_TEST_LOGS_DIR"]).resolve()
    else:
        logs_dir = _root.subdir("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Mirror run.py:66/93 so the host can locate this invocation's output.
    short_name = node_name.removeprefix("ComfyUI-")
    run_dir = logs_dir / f"{short_name}-{timestamp}"

    # Dispatch to platform-specific runner
    if host_platform == "windows":
        rc = _run_windows(args, docker_exe, target_platform, gpu,
                          node_path, node_name, logs_dir, timestamp)
    else:
        rc = _run_linux(args, docker_exe, gpu,
                        node_path, node_name, logs_dir, timestamp)

    # Older container images bake an older comfy-test that doesn't run
    # `git config --global --add safe.directory <bind-mount>` before
    # `git rev-parse HEAD`, so commit_hash silently lands as null in
    # results.json. Patch it on the host where we already know the SHA.
    # Scope to this run's subtree so we don't stamp this SHA onto sibling runs.
    _patch_null_commit_hash(node_path, run_dir)

    # Clean temp clone dir. logs_dir lives outside work_root now, so the rmtree is safe.
    # In URL mode work_root is None -- container did the cloning, nothing to clean here.
    if work_root is not None and not args.keep_clone:
        shutil.rmtree(work_root, ignore_errors=True)
    print(f"[docker run] Logs: {run_dir}")

    return rc


def add_docker_run_parser(subparsers):
    """Register the `docker run` subcommand."""
    p = subparsers.add_parser(
        "run",
        help="Clone a node from URL and run comfy-test in an isolated Docker container",
    )
    p.add_argument("nodelink", help="Git URL (or local path) to the custom node")
    p.add_argument("--branch", "-b", default=None, help="Git branch to clone (default: repo default)")
    p.add_argument("--gpu", action="store_true",
                   help="Enable GPU mode (CUDA passthrough). Default: CPU only.")
    p.add_argument("--portable", action="store_true",
                   help="Windows only: test against portable ComfyUI")
    p.add_argument("--workflow", default=None, help="Run only this specific workflow")
    p.add_argument("--logs-dir", default=None,
                   help="Host directory for logs (default: a temp dir alongside the clone)")
    p.add_argument("--keep-clone", action="store_true",
                   help="Don't delete the cloned node after the run")
    p.add_argument("--persist", action="store_true",
                   help="Bind-mount the workspace to the host so ComfyUI install, "
                        ".venv, and pixi envs survive after the container exits. "
                        "Saved to ~/comfy-test-workspaces/<node>-<HHMM>/. Implies --keep-clone.")

    # Local-Electron Desktop modes: mutually exclusive with each other and
    # with the docker flags. Each picks a host-platform-specific path:
    # macOS opens /Applications/ComfyUI.app, Windows runs the NSIS setup
    # under %LOCALAPPDATA%\Programs\ComfyUI. The runner mirrors the
    # `_test-{macos,windows}-desktop.yml` workflows on the local host so
    # we can iterate on cdp_driver.py without dispatching CI.
    desktop_group = p.add_mutually_exclusive_group()
    desktop_group.add_argument("--desktop_mac", action="store_const",
                               const="mac", dest="desktop_mode",
                               help="Drive ComfyUI Desktop locally on macOS (no docker)")
    desktop_group.add_argument("--desktop_windows", action="store_const",
                               const="windows", dest="desktop_mode",
                               help="Drive ComfyUI Desktop locally on Windows CPU (no docker)")
    desktop_group.add_argument("--desktop_windows_gpu", action="store_const",
                               const="windows_gpu", dest="desktop_mode",
                               help="Drive ComfyUI Desktop locally on Windows with GPU (no docker)")
    p.add_argument("--monitor-progress", type=int, default=None, metavar="PORT",
                   help="Desktop only: serve a live viewer on http://localhost:<PORT>/ "
                        "showing the most recent driver frame + session.log tail. "
                        "Useful while iterating on cdp_driver.py.")
    p.add_argument("--cdp-port", type=int, default=9222, metavar="PORT",
                   help="Desktop only: chromium remote-debugging port the driver "
                        "connects to (default 9222). Bump if the default is held by "
                        "a stale socket from a prior killed run that Windows hasn't "
                        "released yet.")
    p.add_argument("--no-defender-warn", action="store_true",
                   help="Windows: skip the Defender-exclusion check + warning at startup")
    p.set_defaults(func=cmd_docker_run, desktop_mode=None)
