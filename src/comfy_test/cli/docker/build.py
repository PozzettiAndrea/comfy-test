"""`comfy-test docker build` — build the comfy-test GPU image.

OS-agnostic Python orchestrator. Replaces the platform-specific
`docker/{linux,windows}-gpu/build.{sh,ps1}` shell scripts.

Linux flow:
    Stage Dockerfile + entrypoint.sh into a tempdir, run `docker build`,
    smoke-test with `docker run --rm <tag> --help`.

Windows flow:
    Query the host NVIDIA driver via `nvidia-smi`, expect a matching
    `nvidia-driver-<ver>.exe` in $COMFY_TEST_INSTALLERS_DIR. Stage installers +
    Dockerfile + entrypoint.ps1 into $COMFY_TEST_DOCKER_STAGE_DIR, run
    `docker build --isolation=process`, smoke-test torch.cuda.is_available()
    and the entrypoint.

Both flows:
    - If the target tag already exists, prompt before overwriting (unless -y).
    - With --save, `docker save | zstd -19` to $COMFY_TEST_DOCKER_ARTIFACT_PATH
      (replaces the manual save+SMB-push step in the rollout doc).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


DOCKER_IMAGE_LINUX = "comfy-test-linux-gpu:full"
DOCKER_IMAGE_WINDOWS = "comfy-test-windows-gpu:full"
DOCKER_GPU_DEVICE = "class/5B45201D-F2F2-4F3B-85BB-30FF1F953599"

# Repo-relative path to the docker build context (Dockerfile + entrypoint).
# Resolved from this file's location: src/comfy_test/cli/docker/build.py
# → ../../../../../docker/<linux|windows>-gpu
_DOCKER_DIR = Path(__file__).resolve().parents[4] / "docker"


def _find_docker() -> str:
    """Locate `docker` — PATH first, then the Windows default install dir."""
    exe = shutil.which("docker")
    if exe:
        return exe
    if sys.platform == "win32":
        default = r"C:\Program Files\Docker\docker.exe"
        if Path(default).is_file():
            return default
    raise RuntimeError("docker not found on PATH (and not at default Windows install dir)")


def _image_info(docker_exe: str, tag: str) -> Optional[dict]:
    """Return docker image inspect info if the tag exists locally, else None."""
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


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _confirm_overwrite(tag: str, info: dict, force: bool) -> bool:
    """Return True if we should overwrite the existing image."""
    created = info.get("Created", "?")
    size = _human_size(info.get("Size", 0))
    print(f"Image {tag} already exists locally")
    print(f"  Created: {created}")
    print(f"  Size:    {size}")
    if force:
        print("--force / -y passed — overwriting.")
        return True
    if not sys.stdin.isatty():
        print("stdin is not a TTY; pass -y to overwrite non-interactively. Skipping.", file=sys.stderr)
        return False
    answer = input("Overwrite? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _build_linux(args, docker_exe: str) -> int:
    tag = args.tag or DOCKER_IMAGE_LINUX
    src = _DOCKER_DIR / "linux-gpu"
    if not src.is_dir():
        print(f"Build context not found at {src}", file=sys.stderr)
        return 2

    info = _image_info(docker_exe, tag)
    if info and not _confirm_overwrite(tag, info, args.force):
        return 0

    with tempfile.TemporaryDirectory(prefix="comfy-test-build-") as tmp:
        stage = Path(tmp)
        shutil.copy(src / "Dockerfile", stage / "Dockerfile")
        shutil.copy(src / "entrypoint.sh", stage / "entrypoint.sh")
        print(f"[docker build] staging to {stage}")
        print(f"[docker build] building {tag} ...")
        rc = subprocess.run(
            [docker_exe, "build", "-t", tag, "-f", str(stage / "Dockerfile"), str(stage)],
        ).returncode
        if rc != 0:
            print(f"docker build failed (exit {rc})", file=sys.stderr)
            return rc

    if not args.no_smoke:
        print("[docker build] smoke: comfy-test --help")
        rc = subprocess.run([docker_exe, "run", "--rm", tag, "--help"]).returncode
        if rc != 0:
            print(f"smoke test failed (exit {rc})", file=sys.stderr)
            return rc

    if args.save:
        return _save_image(docker_exe, tag, args)
    return 0


def _query_host_driver_windows() -> Optional[str]:
    """Return the host NVIDIA driver version via nvidia-smi, or None."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        first = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
        return first or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _build_windows(args, docker_exe: str) -> int:
    tag = args.tag or DOCKER_IMAGE_WINDOWS
    src = _DOCKER_DIR / "windows-gpu"
    if not src.is_dir():
        print(f"Build context not found at {src}", file=sys.stderr)
        return 2

    # Driver-match guard.
    nvidia_exe = args.nvidia_exe
    if not nvidia_exe:
        host_drv = _query_host_driver_windows()
        if not host_drv:
            print("Could not query host NVIDIA driver via nvidia-smi. "
                  "Install the driver on the host or pass --nvidia-exe.", file=sys.stderr)
            return 2
        nvidia_exe = f"nvidia-driver-{host_drv}.exe"
        print(f"[docker build] host driver: {host_drv} → expecting {nvidia_exe}")

    installers_dir = Path(os.environ.get("COMFY_TEST_INSTALLERS_DIR",
                                          r"\\192.168.1.19\pxe\scripts\installers"))
    nv_src = installers_dir / nvidia_exe
    if not nv_src.is_file():
        print(f"Driver installer not found at {nv_src}.\n"
              f"Container driver must match host driver exactly. Stage {nvidia_exe} "
              f"in {installers_dir} (or pass --nvidia-exe matching a staged file) and retry.",
              file=sys.stderr)
        return 3

    git_exe = args.git_exe
    git_src = installers_dir / git_exe
    if not git_src.is_file():
        print(f"Git installer not found at {git_src}. Stage it or pass --git-exe.",
              file=sys.stderr)
        return 3

    info = _image_info(docker_exe, tag)
    if info and not _confirm_overwrite(tag, info, args.force):
        return 0

    stage_dir = Path(os.environ.get("COMFY_TEST_DOCKER_STAGE_DIR", r"D:\docker-stage")) / "windows-gpu"
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Stage installers (skip-copy if already present)
    for src_file, name in ((nv_src, nvidia_exe), (git_src, git_exe)):
        dst = stage_dir / name
        if not dst.is_file():
            print(f"[docker build] copy {name} → {stage_dir}")
            shutil.copy(src_file, dst)
        else:
            print(f"[docker build] {name} already staged")

    # Stage Dockerfile + entrypoint
    shutil.copy(src / "Dockerfile", stage_dir / "Dockerfile")
    shutil.copy(src / "entrypoint.ps1", stage_dir / "entrypoint.ps1")

    print(f"[docker build] building {tag} (--isolation=process) ...")
    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "0"  # static Moby ships without buildx
    rc = subprocess.run([
        docker_exe, "build",
        "--isolation=process",
        "--build-arg", f"NVIDIA_INSTALLER={nvidia_exe}",
        "--build-arg", f"GIT_INSTALLER={git_exe}",
        "-t", tag,
        "-f", str(stage_dir / "Dockerfile"),
        str(stage_dir),
    ], env=env).returncode
    if rc != 0:
        print(f"docker build failed (exit {rc})", file=sys.stderr)
        return rc

    if not args.no_smoke:
        print("\n[docker build] smoke 1: torch.cuda.is_available() inside the image")
        # Install torch into a throwaway uv venv inside the container, just for the smoke.
        cuda_check = (
            "$ErrorActionPreference='Stop';"
            "$env:PATH = 'C:\\Users\\ContainerAdministrator\\.local\\bin;' + $env:PATH;"
            "uv venv --python 3.10 C:\\smoke-venv | Out-Null;"
            "& C:\\smoke-venv\\Scripts\\activate.ps1;"
            "uv pip install --no-cache torch --index-url https://download.pytorch.org/whl/cu128 | Out-Null;"
            "python -c \"import torch; print('torch', torch.__version__);"
            " print('cuda?', torch.cuda.is_available());"
            " print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU')\""
        )
        rc = subprocess.run([
            docker_exe, "run", "--rm",
            "--isolation=process",
            "--device", DOCKER_GPU_DEVICE,
            "--entrypoint", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            tag, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cuda_check,
        ]).returncode
        if rc != 0:
            print(f"\nCUDA smoke test failed (exit {rc}) — host/container driver mismatch?",
                  file=sys.stderr)
            return rc

        print("\n[docker build] smoke 2: comfy-test --help")
        rc = subprocess.run([
            docker_exe, "run", "--rm",
            "--isolation=process",
            "--device", DOCKER_GPU_DEVICE,
            tag, "--help",
        ]).returncode
        if rc != 0:
            print(f"entrypoint smoke failed (exit {rc})", file=sys.stderr)
            return rc

    print(f"\n[docker build] {tag} built and smoke-tested successfully.")
    if args.save:
        return _save_image(docker_exe, tag, args)
    print(f"To roll out cluster-wide: comfy-test docker build --save (writes to "
          f"$COMFY_TEST_DOCKER_ARTIFACT_PATH).")
    return 0


def _save_image(docker_exe: str, tag: str, args) -> int:
    """`docker save | zstd -19` to $COMFY_TEST_DOCKER_ARTIFACT_PATH."""
    artifact = args.artifact_path or os.environ.get("COMFY_TEST_DOCKER_ARTIFACT_PATH")
    if not artifact:
        print("--save requires COMFY_TEST_DOCKER_ARTIFACT_PATH (or --artifact-path).",
              file=sys.stderr)
        return 4
    artifact_path = Path(artifact)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[docker build] saving {tag} → {artifact_path}")
    if not shutil.which("zstd"):
        print("zstd not found on PATH; install zstd and retry.", file=sys.stderr)
        return 5
    save_proc = subprocess.Popen([docker_exe, "save", tag], stdout=subprocess.PIPE)
    zstd_proc = subprocess.Popen(
        ["zstd", "-19", "-o", str(artifact_path)],
        stdin=save_proc.stdout,
    )
    save_proc.stdout.close()  # let zstd see EOF if save fails
    zstd_rc = zstd_proc.wait()
    save_rc = save_proc.wait()
    if save_rc != 0 or zstd_rc != 0:
        print(f"save failed: docker save={save_rc}, zstd={zstd_rc}", file=sys.stderr)
        return save_rc or zstd_rc
    sz = artifact_path.stat().st_size
    print(f"[docker build] wrote {_human_size(sz)} to {artifact_path}")
    return 0


def cmd_docker_build(args) -> int:
    """Entry point for `comfy-test docker build`."""
    try:
        docker_exe = _find_docker()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if sys.platform == "win32":
        return _build_windows(args, docker_exe)
    if sys.platform.startswith("linux"):
        return _build_linux(args, docker_exe)
    print(f"Unsupported host platform: {sys.platform}", file=sys.stderr)
    return 2


def add_docker_build_parser(subparsers):
    """Register the `docker build` subcommand."""
    p = subparsers.add_parser(
        "build",
        help="Build the comfy-test GPU image (auto-detects Linux vs Windows host)",
    )
    p.add_argument("--tag", default=None,
                   help="Image tag (default: comfy-test-{linux,windows}-gpu:full)")
    p.add_argument("-y", "--force", action="store_true",
                   help="Overwrite existing image without prompting")
    p.add_argument("--no-smoke", action="store_true",
                   help="Skip post-build smoke tests")
    p.add_argument("--save", action="store_true",
                   help="After build, docker save | zstd -19 to "
                        "$COMFY_TEST_DOCKER_ARTIFACT_PATH (or --artifact-path)")
    p.add_argument("--artifact-path", default=None,
                   help="Override $COMFY_TEST_DOCKER_ARTIFACT_PATH for --save")
    # Windows-only knobs (ignored on Linux):
    p.add_argument("--nvidia-exe", default=None,
                   help="Windows: name of the driver installer in "
                        "$COMFY_TEST_INSTALLERS_DIR (default: nvidia-driver-<host_driver>.exe)")
    p.add_argument("--git-exe", default="Git-2.53.0-64-bit.exe",
                   help="Windows: name of the Git installer in $COMFY_TEST_INSTALLERS_DIR")
    p.set_defaults(func=cmd_docker_build)
