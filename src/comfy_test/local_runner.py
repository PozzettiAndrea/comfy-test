"""Local test execution via act (GitHub Actions locally)."""

import os
import platform
import subprocess
import shutil
import tempfile
import time
import re
import sys
from pathlib import Path
from typing import Callable, Optional, List, Tuple

# Container images
LINUX_IMAGE = "catthehacker/ubuntu:act-22.04"
WINDOWS_IMAGE = "python:3.12-windowsservercore-ltsc2022"
WINDOWS_IMAGE_1809 = "python:3.12-windowsservercore-1809"  # For Windows 10

# Base images with git, uv, ComfyUI pre-installed (local names)
WINDOWS_BASE_IMAGE = "comfy-test-base:windows-ltsc2022"
WINDOWS_BASE_IMAGE_1809 = "comfy-test-base:windows-1809"

# Remote base images on Docker Hub (pulled if local not found)
WINDOWS_BASE_IMAGE_REMOTE = "pozzettiandrea/comfy-test-base:windows-ltsc2022"
WINDOWS_BASE_IMAGE_1809_REMOTE = "pozzettiandrea/comfy-test-base:windows-1809"

# Git installer command (direct download, no chocolatey)
GIT_INSTALL_CMD = (
    '$gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe"; '
    'Invoke-WebRequest -Uri $gitUrl -OutFile $env:TEMP\\Git-installer.exe; '
    'Start-Process -FilePath $env:TEMP\\Git-installer.exe -ArgumentList "/VERYSILENT","/NORESTART","/NOCANCEL","/SP-" -Wait; '
    '$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")'
)


def get_windows_build() -> int:
    """Get Windows build number."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion').CurrentBuild"],
            capture_output=True, text=True
        )
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


def get_windows_container_image() -> str:
    """Get appropriate Windows container image for host OS version."""
    # Windows 10 (builds 17763-19045) needs 1809 image
    # Windows 11 / Server 2022 (builds 20000+) can use ltsc2022
    if get_windows_build() < 20000:
        return WINDOWS_IMAGE_1809
    else:
        return WINDOWS_IMAGE


def get_windows_base_image() -> str:
    """Get appropriate Windows base image name for host OS version."""
    if get_windows_build() < 20000:
        return WINDOWS_BASE_IMAGE_1809
    else:
        return WINDOWS_BASE_IMAGE


def get_windows_base_image_remote() -> str:
    """Get remote Docker Hub image name for host OS version."""
    if get_windows_build() < 20000:
        return WINDOWS_BASE_IMAGE_1809_REMOTE
    else:
        return WINDOWS_BASE_IMAGE_REMOTE


def windows_base_image_exists() -> bool:
    """Check if the Windows base image exists locally."""
    image_name = get_windows_base_image()
    result = subprocess.run(
        ["docker", "images", "-q", image_name],
        capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def pull_windows_base_image(log: Callable[[str], None]) -> bool:
    """Try to pull the base image from Docker Hub. Returns True if successful."""
    remote_image = get_windows_base_image_remote()
    local_image = get_windows_base_image()

    log(f"Pulling {remote_image} from Docker Hub...")
    result = subprocess.run(
        ["docker", "pull", remote_image],
        capture_output=False  # Show progress
    )

    if result.returncode == 0:
        # Tag as local name for consistency
        subprocess.run(["docker", "tag", remote_image, local_image], check=True)
        log(f"Tagged as {local_image}")
        return True
    return False


def build_windows_base_image(log: Callable[[str], None], force: bool = False) -> str:
    """Build base Windows image with git, uv, ComfyUI, and deps.

    Returns the image name.
    """
    image_name = get_windows_base_image()

    # Check if already exists locally
    if not force and windows_base_image_exists():
        log(f"Base image {image_name} already exists (use --rebuild to force)")
        return image_name

    # Try to pull from Docker Hub first (faster than building)
    if not force:
        log("Checking Docker Hub for pre-built image...")
        if pull_windows_base_image(log):
            log(f"Pulled {image_name} from Docker Hub")
            return image_name
        log("Not found on Docker Hub, building locally...")

    log(f"Building base image {image_name}...")
    log("This will take ~15 minutes the first time (git install + ComfyUI deps)")

    base_image = get_windows_container_image()

    # Ensure Docker is in Windows mode
    docker_os = get_docker_os()
    if docker_os != "windows":
        log(f"Docker is in {docker_os} mode, switching to Windows containers...")
        if switch_docker_to_windows():
            for _ in range(30):
                time.sleep(2)
                if get_docker_os() == "windows":
                    break
            else:
                raise RuntimeError("Docker failed to switch to Windows containers")
        else:
            raise RuntimeError("Could not switch Docker to Windows containers")

    # Create container
    log(f"Creating container from {base_image}...")
    result = subprocess.run(
        ["docker", "create", "-t", "--name", f"comfy-base-build-{int(time.time())}",
         base_image, "powershell", "-NoExit", "-Command", "Start-Sleep -Seconds 7200"],
        capture_output=True, text=True, check=True
    )
    container_id = result.stdout.strip()
    log(f"Container: {container_id[:12]}")

    try:
        # Start container
        log("Starting container...")
        subprocess.run(["docker", "start", container_id], check=True, capture_output=True)

        def docker_exec(cmd: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["docker", "exec", container_id, "powershell", "-Command", cmd],
                capture_output=True, text=True, encoding='utf-8', errors='replace'
            )

        def docker_exec_stream(cmd: str) -> int:
            proc = subprocess.Popen(
                ["docker", "exec", container_id, "powershell", "-Command", cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace'
            )
            for line in proc.stdout:
                log(f"  {line.rstrip()}")
            return proc.wait()

        # Install git
        log("Installing git (this takes ~10 minutes)...")
        docker_exec_stream(GIT_INSTALL_CMD)

        # Verify git
        result = docker_exec("git --version")
        if result.returncode != 0:
            raise RuntimeError("Git installation failed")
        log(f"  {result.stdout.strip()}")

        # Install uv
        log("Installing uv...")
        docker_exec_stream("pip install uv")

        # Clone ComfyUI
        log("Cloning ComfyUI...")
        docker_exec_stream("git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git C:/ComfyUI")

        # Install deps
        log("Installing ComfyUI dependencies (uv)...")
        docker_exec_stream("uv pip install --system -r C:/ComfyUI/requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu")

        # Install playwright for screenshots
        log("Installing playwright and chromium...")
        docker_exec_stream("uv pip install --system playwright")
        docker_exec_stream("playwright install chromium")

        # Stop container before commit (Windows doesn't support committing running containers)
        log("Stopping container...")
        subprocess.run(["docker", "stop", container_id], check=True, capture_output=True)

        # Commit as new image
        log(f"Committing as {image_name}...")
        subprocess.run(["docker", "commit", container_id, image_name], check=True)

        log(f"Built {image_name}")
        return image_name

    finally:
        # Cleanup
        log("Cleaning up build container...")
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


# For backwards compatibility
ACT_IMAGE = LINUX_IMAGE


def get_docker_os() -> str:
    """Detect Docker's container OS mode (linux or windows)."""
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Os}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip().lower()
    except Exception:
        pass
    # Fallback to host OS
    return "windows" if platform.system() == "Windows" else "linux"


def switch_docker_to_windows() -> bool:
    """Switch Docker Desktop to Windows containers. Returns True if successful."""
    docker_cli = Path("C:/Program Files/Docker/Docker/DockerCli.exe")
    if not docker_cli.exists():
        return False
    try:
        result = subprocess.run(
            [str(docker_cli), "-SwitchWindowsEngine"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def switch_docker_to_linux() -> bool:
    """Switch Docker Desktop to Linux containers. Returns True if successful."""
    docker_cli = Path("C:/Program Files/Docker/Docker/DockerCli.exe")
    if not docker_cli.exists():
        return False
    try:
        result = subprocess.run(
            [str(docker_cli), "-SwitchLinuxEngine"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_gitignore(node_dir: Path, pattern: str = ".comfy-test-logs/"):
    """Add pattern to .gitignore if not already present."""
    gitignore = node_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if pattern.rstrip('/') not in content:
            with open(gitignore, "a", encoding="utf-8") as f:
                f.write(f"\n# comfy-test output\n{pattern}\n")
    else:
        gitignore.write_text(f"# comfy-test output\n{pattern}\n", encoding="utf-8")


# Patterns to detect step transitions in act output (no ^ anchor - lines have leading whitespace)
STEP_START = re.compile(r'Run (?:Main |Post )?(.+)$')
STEP_SUCCESS = re.compile(r'Success - (?:Main |Post )?(.+?) \[')
STEP_FAILURE = re.compile(r'Failure - (?:Main |Post )?(.+?) \[')


def _gitignore_filter(base_dir: Path):
    """Create a shutil.copytree ignore function based on .gitignore patterns."""
    import fnmatch

    # Always ignore these (essential for clean copy)
    # Note: .git is NOT ignored - workflow needs it for checkout step
    always_ignore = {'__pycache__', '.comfy-test', '.comfy-test-env', '.comfy-test-logs'}

    # Parse .gitignore if it exists
    gitignore_patterns = []
    gitignore_file = base_dir / ".gitignore"
    if gitignore_file.exists():
        for line in gitignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Remove trailing slashes (we match both files and dirs)
            pattern = line.rstrip('/')
            gitignore_patterns.append(pattern)

    def ignore_func(directory: str, names: List[str]) -> List[str]:
        ignored = []
        rel_dir = Path(directory).relative_to(base_dir) if directory != str(base_dir) else Path('.')

        for name in names:
            # Always ignore these
            if name in always_ignore:
                ignored.append(name)
                continue

            # Check gitignore patterns
            rel_path = rel_dir / name
            for pattern in gitignore_patterns:
                # Match against filename and relative path
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel_path), pattern):
                    ignored.append(name)
                    break
                # Handle patterns like "dir/" matching directories
                if pattern.endswith('/') and fnmatch.fnmatch(name, pattern[:-1]):
                    ignored.append(name)
                    break

        return ignored

    return ignore_func


def split_log_by_workflow(log_file: Path, logs_dir: Path) -> int:
    """Extract per-workflow sections from main log file."""
    if not log_file.exists():
        return 0

    content = log_file.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Match: "executing mesh_info.json [1/23]"
    workflow_start = re.compile(r'executing (\S+)\.json\s+\[\d+/\d+\]')
    # Match: "mesh_info.json [1/23] - PASS" or "- FAIL"
    workflow_end = re.compile(r'\.json\s+\[\d+/\d+\]\s+-\s+(PASS|FAIL)')

    logs_dir.mkdir(parents=True, exist_ok=True)

    current_workflow = None
    current_lines = []
    count = 0

    for line in lines:
        match = workflow_start.search(line)
        if match:
            if current_workflow and current_lines:
                (logs_dir / f"{current_workflow}.log").write_text("\n".join(current_lines), encoding="utf-8")
                count += 1
            current_workflow = match.group(1)
            current_lines = [line]
        elif current_workflow:
            current_lines.append(line)
            if workflow_end.search(line):
                (logs_dir / f"{current_workflow}.log").write_text("\n".join(current_lines), encoding="utf-8")
                count += 1
                current_workflow = None
                current_lines = []

    return count


def run_windows_docker(
    node_dir: Path,
    output_dir: Path,
    config_file: str,
    gpu: bool,
    log: Callable[[str], None],
) -> int:
    """Run Windows tests directly in Docker (bypasses act which has Windows container issues).

    Uses a pre-built base image with git, uv, ComfyUI and deps for fast startup.
    Auto-builds the base image on first run.
    """
    # Get or build the base image (has git, uv, ComfyUI, deps pre-installed)
    try:
        container_image = build_windows_base_image(log)
    except Exception as e:
        log(f"Error building base image: {e}")
        return 1

    log(f"Using base image: {container_image}")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"{output_dir.name}.log"

    # Build wheels on host first
    is_windows_host = platform.system() == "Windows"
    if is_windows_host:
        local_comfy_test = Path.home() / "Desktop" / "utils" / "comfy-test"
        local_comfy_env = Path.home() / "Desktop" / "utils" / "comfy-env"
    else:
        local_comfy_test = Path.home() / "utils" / "comfy-test"
        local_comfy_env = Path.home() / "utils" / "comfy-env"

    # Create temp directory for wheels
    wheel_dir = Path(tempfile.mkdtemp(prefix="comfy-wheels-"))

    try:
        if local_comfy_test.exists():
            log("Building comfy-test wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_test) + "[screenshot]", "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                check=True
            )

        if local_comfy_env.exists():
            log("Building comfy-env wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_env), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                check=True
            )

        # Create container from base image
        log("Creating Windows container...")
        try:
            result = subprocess.run(
                ["docker", "create", "-t", "--name", f"comfy-test-{int(time.time())}",
                 container_image, "powershell", "-NoExit", "-Command", "Start-Sleep -Seconds 3600"],
                capture_output=True, text=True, check=True
            )
            container_id = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            log(f"Docker create failed:")
            if e.stdout:
                log(f"  stdout: {e.stdout}")
            if e.stderr:
                log(f"  stderr: {e.stderr}")
            raise
        log(f"Container: {container_id[:12]}")

        try:
            # Copy files BEFORE starting (Hyper-V containers don't support cp while running)
            # Copy node to custom_nodes directly (base image already has ComfyUI)
            log("Copying node to container (respecting .gitignore)...")
            temp_node_dir = Path(tempfile.mkdtemp(prefix="comfy-node-")) / node_dir.name
            shutil.copytree(node_dir, temp_node_dir, ignore=_gitignore_filter(node_dir))
            subprocess.run(["docker", "cp", str(temp_node_dir), f"{container_id}:C:/ComfyUI/custom_nodes/{node_dir.name}"], check=True)
            shutil.rmtree(temp_node_dir.parent, ignore_errors=True)

            log("Copying wheels to container...")
            subprocess.run(["docker", "cp", str(wheel_dir), f"{container_id}:C:/wheels"], check=True)

            # Copy config file directly to ComfyUI (base image already has it)
            config_src = node_dir / config_file
            if config_src.exists():
                log("Copying config file...")
                subprocess.run(["docker", "cp", str(config_src), f"{container_id}:C:/ComfyUI/{config_file}"], check=True)

            # Now start the container
            log("Starting container...")
            subprocess.run(["docker", "start", container_id], check=True, capture_output=True)

            def docker_exec(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
                """Execute command in container."""
                return subprocess.run(
                    ["docker", "exec", container_id, "powershell", "-Command", cmd],
                    capture_output=True, text=True, check=check, encoding='utf-8', errors='replace'
                )

            def docker_exec_stream(cmd: str) -> int:
                """Execute command in container with streaming output."""
                proc = subprocess.Popen(
                    ["docker", "exec", container_id, "powershell", "-Command", cmd],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace'
                )
                with open(log_file, "a", encoding="utf-8") as f:
                    for line in proc.stdout:
                        log(f"  {line.rstrip()}")
                        f.write(line)
                return proc.wait()

            # Base image already has git, uv, ComfyUI, and deps - just install wheels
            log("Installing comfy-test and comfy-env...")
            docker_exec_stream("uv pip install --system (Get-ChildItem C:/wheels/*.whl).FullName")

            # Install custom node dependencies if requirements.txt exists
            node_name = node_dir.name
            log("Installing custom node dependencies...")
            docker_exec_stream(
                f"if (Test-Path C:/ComfyUI/custom_nodes/{node_name}/requirements.txt) {{ "
                f"uv pip install --system -r C:/ComfyUI/custom_nodes/{node_name}/requirements.txt "
                f"}}"
            )

            # Run install.py if it exists
            docker_exec_stream(
                f"if (Test-Path C:/ComfyUI/custom_nodes/{node_name}/install.py) {{ "
                f"cd C:/ComfyUI/custom_nodes/{node_name}; python install.py "
                f"}}"
            )

            # Run comfy-test from the custom node directory
            # COMFY_TEST_IN_DOCKER=1 signals we're in the container (skip Docker mode)
            # PYTHONUNBUFFERED=1 forces output to flush immediately
            log("Running tests...")
            exit_code = docker_exec_stream(
                f"$env:COMFY_TEST_IN_DOCKER='1'; $env:PYTHONUNBUFFERED='1'; "
                f"cd C:/ComfyUI/custom_nodes/{node_name}; python -u -m comfy_test run --platform windows"
            )

            # Copy results out (from custom node directory)
            log("Copying results...")
            subprocess.run(
                ["docker", "cp", f"{container_id}:C:/ComfyUI/custom_nodes/{node_name}/.comfy-test/.", str(output_dir)],
                capture_output=True  # May fail if no results
            )

            log(f"\nLog: {log_file}")
            return exit_code

        finally:
            # Cleanup container
            log("Cleaning up container...")
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

    finally:
        # Cleanup wheel directory
        shutil.rmtree(wheel_dir, ignore_errors=True)


def run_linux_pooled(
    container_id: str,
    node_dir: Path,
    output_dir: Path,
    config_file: str,
    gpu: bool,
    log: Callable[[str], None],
) -> int:
    """Run Linux test in a pre-created pooled container.

    This bypasses act entirely for faster startup when a pooled container is available.
    The container is destroyed after the test completes.
    """
    import tempfile

    node_name = node_dir.name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create wheel directory
    wheel_dir = Path(tempfile.mkdtemp(prefix="comfy-wheels-"))

    try:
        # Build wheels for comfy-test and comfy-env
        log("Building wheels...")
        local_comfy_test = Path.home() / "utils" / "comfy-test"
        if local_comfy_test.exists():
            subprocess.run(
                ["pip", "wheel", str(local_comfy_test), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        local_comfy_env = Path.home() / "utils" / "comfy-env"
        if local_comfy_env.exists():
            subprocess.run(
                ["pip", "wheel", str(local_comfy_env), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        local_comfy_3d_viewers = Path.home() / "utils" / "comfy-3d-viewers"
        if local_comfy_3d_viewers.exists():
            subprocess.run(
                ["pip", "wheel", str(local_comfy_3d_viewers), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        # Copy node to container (respecting .gitignore)
        log("Copying files to container...")
        temp_node_dir = Path(tempfile.mkdtemp(prefix="comfy-node-")) / node_dir.name
        _copy_with_gitignore(node_dir, temp_node_dir)

        # Create work directory in container
        subprocess.run(
            ["docker", "exec", container_id, "mkdir", "-p", "/work"],
            check=True, capture_output=True
        )

        # Copy files into container
        subprocess.run(
            ["docker", "cp", str(temp_node_dir), f"{container_id}:/work/node"],
            check=True
        )
        subprocess.run(
            ["docker", "cp", str(wheel_dir), f"{container_id}:/work/wheels"],
            check=True
        )

        # Copy playwright cache if exists
        playwright_cache = Path.home() / ".cache" / "ms-playwright"
        if playwright_cache.exists():
            subprocess.run(
                ["docker", "cp", str(playwright_cache), f"{container_id}:/root/.cache/ms-playwright"],
                capture_output=True  # May fail, non-fatal
            )

        # Helper to run commands in container with streaming output
        def docker_exec_stream(cmd: str) -> int:
            proc = subprocess.Popen(
                ["docker", "exec", container_id, "bash", "-c", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in iter(proc.stdout.readline, ''):
                log(f"  | {line.rstrip()}")
            return proc.wait()

        # Install dependencies
        log("Installing uv...")
        docker_exec_stream("pip install uv -q")

        log("Installing wheels...")
        docker_exec_stream("uv pip install --system /work/wheels/*.whl")

        log("Installing Playwright...")
        docker_exec_stream("pip install playwright pillow -q && playwright install chromium --with-deps")

        # Run tests
        log("Running tests...")
        exit_code = docker_exec_stream(
            f"cd /work/node && python -u -m comfy_test run --platform linux"
        )

        # Copy results back
        log("Copying results...")
        subprocess.run(
            ["docker", "cp", f"{container_id}:/work/node/.comfy-test/.", str(output_dir)],
            capture_output=True  # May fail if no results
        )

        # Cleanup temp directories
        shutil.rmtree(temp_node_dir.parent, ignore_errors=True)

        return exit_code

    finally:
        # Always destroy the container after use (keep pool virgin)
        log("Destroying container...")
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        # Cleanup wheel directory
        shutil.rmtree(wheel_dir, ignore_errors=True)


def run_local(
    node_dir: Path,
    output_dir: Path,
    config_file: str = "comfy-test.toml",
    gpu: bool = False,
    verbose: bool = False,
    log_callback: Optional[Callable[[str], None]] = None,
    platform_name: Optional[str] = None,
) -> int:
    """Run tests locally via act (GitHub Actions in Docker).

    Args:
        node_dir: Path to the custom node directory
        output_dir: Where to save screenshots/logs/results.json
        config_file: Config file name
        gpu: Enable GPU passthrough
        verbose: Show all output (streaming mode)
        log_callback: Function to call with log lines
        platform_name: Platform to test (linux, windows, windows-portable). Auto-detected if None.

    Returns:
        Exit code (0 = success)
    """
    log = log_callback or print

    # Auto-detect platform if not specified
    is_windows_host = platform.system() == "Windows"
    if platform_name is None:
        platform_name = "windows" if is_windows_host else "linux"

    # For Windows platforms, use direct Docker (act has Windows container issues)
    if platform_name in ("windows", "windows-portable"):
        return run_windows_docker(node_dir, output_dir, config_file, gpu, log)

    # Linux: check if container pool has a ready container for fast startup
    if platform_name == "linux":
        from .container_pool import ContainerPool
        pool = ContainerPool()
        pooled_container = pool.acquire()
        if pooled_container:
            log(f"Using pooled container: {pooled_container[:12]}")
            return run_linux_pooled(
                container_id=pooled_container,
                node_dir=node_dir,
                output_dir=output_dir,
                config_file=config_file,
                gpu=gpu,
                log=log,
            )

    # Linux: use act (works well with Linux containers)
    # Check for Docker mode mismatch and auto-switch if needed
    docker_os = get_docker_os()
    if platform_name in ("windows", "windows-portable") and docker_os != "windows":
        log(f"Docker is in {docker_os} mode, switching to Windows containers...")
        if switch_docker_to_windows():
            log("Switched to Windows containers")
            # Wait for Docker to be ready
            for i in range(30):
                time.sleep(2)
                if get_docker_os() == "windows":
                    break
            else:
                log("Error: Docker failed to switch to Windows containers")
                return 1
        else:
            log("Error: Could not switch Docker to Windows containers (Docker Desktop required)")
            return 1
    elif platform_name == "linux" and docker_os != "linux":
        log(f"Docker is in {docker_os} mode, switching to Linux containers...")
        if switch_docker_to_linux():
            log("Switched to Linux containers")
            for i in range(30):
                time.sleep(2)
                if get_docker_os() == "linux":
                    break
            else:
                log("Error: Docker failed to switch to Linux containers")
                return 1
        else:
            log("Error: Could not switch Docker to Linux containers (Docker Desktop required)")
            return 1

    # Determine container image and job name
    if platform_name in ("windows", "windows-portable"):
        container_image = WINDOWS_IMAGE
        job_name = f"test-{platform_name}"
        runner_label = "windows-latest"
    else:
        container_image = LINUX_IMAGE
        job_name = "test-linux"
        runner_label = "ubuntu-latest"

    log(f"Platform: {platform_name} (image: {container_image})")

    # Auto-add .comfy-test-logs to .gitignore
    ensure_gitignore(node_dir, ".comfy-test-logs/")

    # Verify act is installed
    if not shutil.which("act"):
        log("Error: act is not installed. Install from https://github.com/nektos/act")
        return 1

    # Verify node directory has config
    if not (node_dir / config_file).exists():
        log(f"Error: {config_file} not found in {node_dir}")
        return 1

    # Verify workflow file exists
    workflow_file = node_dir / ".github" / "workflows" / "run-tests.yml"
    if not workflow_file.exists():
        log(f"Error: {workflow_file} not found")
        return 1

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create main log file inside output_dir
    log_file = output_dir / f"{output_dir.name}.log"

    # Local paths (Desktop/utils on Windows, ~/utils on Linux)
    if is_windows_host:
        local_comfy_test = Path.home() / "Desktop" / "utils" / "comfy-test"
        local_comfy_env = Path.home() / "Desktop" / "utils" / "comfy-env"
    else:
        local_comfy_test = Path.home() / "utils" / "comfy-test"
        local_comfy_env = Path.home() / "utils" / "comfy-env"
    local_workflow = local_comfy_test / ".github" / "workflows" / "test-matrix-local.yml"

    # Create isolated temp directory for full isolation
    temp_dir = tempfile.mkdtemp(prefix="comfy-test-")
    work_dir = Path(temp_dir) / node_dir.name

    try:
        # Copy node to temp dir, respecting .gitignore
        log(f"Copying node to isolated environment...")
        shutil.copytree(
            node_dir, work_dir,
            ignore=_gitignore_filter(node_dir)
        )

        # Set up local workflow - copy test-matrix-local.yml directly
        if local_workflow.exists():
            work_workflow_dir = work_dir / ".github" / "workflows"
            work_workflow_dir.mkdir(parents=True, exist_ok=True)
            target = work_workflow_dir / "test-matrix.yml"
            shutil.copy(local_workflow, target)
            # Make workflow file unique per run to ensure unique container/volume names
            # (act derives container name hash from workflow content, volume from job name)
            run_id = Path(temp_dir).name
            short_id = run_id.split("-")[-1][:8]  # e.g. "rpjqwysv" from "comfy-test-rpjqwysv"
            content = target.read_text()
            content = f"# run-id: {run_id}\n" + content
            # Make job names unique to avoid volume collisions in parallel runs
            content = content.replace("test-linux:", f"test-linux-{short_id}:")
            content = content.replace("parse-config:", f"parse-config-{short_id}:")
            content = content.replace("needs: parse-config", f"needs: parse-config-{short_id}")
            content = content.replace("needs.parse-config.", f"needs.parse-config-{short_id}.")
            target.write_text(content)
            # Update job_name to match the modified workflow
            job_name = f"{job_name}-{short_id}"

        # Pre-build wheels on host (avoids hatchling issues in Docker)
        wheel_dir = work_dir / ".wheels"
        wheel_dir.mkdir(exist_ok=True)

        if local_comfy_test.exists():
            log(f"Building comfy-test wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_test) + "[screenshot]", "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                check=True
            )

        if local_comfy_env.exists():
            log(f"Building comfy-env wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_env), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                check=True
            )

        local_comfy_3d_viewers = Path.home() / "utils" / "comfy-3d-viewers"
        if local_comfy_3d_viewers.exists():
            log(f"Building comfy-3d-viewers wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_3d_viewers), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        # Build container options - mount output dir
        playwright_cache = Path.home() / ".cache" / "ms-playwright"
        container_opts = [
            "-t",  # Allocate pseudo-TTY to force line-buffered output
            f"-v {output_dir}:{work_dir}/.comfy-test",
            f"-v {playwright_cache}:/root/.cache/ms-playwright",  # Cache Playwright browsers
            "--shm-size=8g",  # Default 64MB is too small for ML tensor transfer
            "--memory=8g",  # Limit memory to allow parallel test runs
        ]
        if gpu:
            container_opts.append("--gpus all")

        # Use unique action cache per run to enable concurrent execution
        action_cache_dir = Path(temp_dir) / ".cache" / "act"
        # Use unique toolcache path to isolate concurrent runs
        toolcache_path = f"/tmp/toolcache-{Path(temp_dir).name}" if not is_windows_host else f"C:\\temp\\toolcache-{Path(temp_dir).name}"

        # Base command differs by host OS
        if is_windows_host:
            # Windows: no stdbuf
            cmd = ["act"]
        else:
            # Linux: use stdbuf for line-buffered output
            cmd = ["stdbuf", "-oL", "act"]

        cmd.extend([
            "-W", ".github/workflows/test-matrix.yml",
            "-P", f"{runner_label}={container_image}",
            "--pull=false",
            "--rm",
            "-j", job_name,
            "--network", "bridge",
            "--action-cache-path", str(action_cache_dir),
            "--container-options", " ".join(container_opts),
            "--env", "PYTHONUNBUFFERED=1",
            "--env", f"RUNNER_TOOL_CACHE={toolcache_path}",
        ])
        if gpu:
            cmd.extend(["--env", "COMFY_TEST_GPU=1"])

        job_prefix_pattern = re.compile(r'\[test/[^\]]+\]\s*')
        # Detect workflow execution lines: "executing mesh_info.json [1/23]"
        workflow_pattern = re.compile(r'executing (\S+)\s+\[(\d+)/(\d+)\]')

        start_time = time.time()

        # Run with unbuffered output from isolated work_dir
        process = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            encoding='utf-8',
            errors='replace',
        )

        # Track steps for summary mode
        current_step = None
        current_step_output: List[str] = []
        completed_steps: List[Tuple[str, bool, List[str]]] = []
        return_code = 0

        try:
            with open(log_file, "w", encoding="utf-8", errors="replace") as f:
                while True:
                    if process.stdout:
                        line = process.stdout.readline()
                        if line:
                            # Strip job prefix
                            clean_line = job_prefix_pattern.sub('', line.rstrip())
                            elapsed = int(time.time() - start_time)
                            mins, secs = divmod(elapsed, 60)
                            timer = f"[{mins:02d}:{secs:02d}]"
                            formatted = f"{timer} {clean_line}"

                            # Write to log file
                            f.write(formatted + "\n")
                            f.flush()

                            if verbose:
                                # Verbose mode: stream everything
                                log(formatted)
                            else:
                                # Summary mode: track steps
                                if workflow_match := workflow_pattern.search(clean_line):
                                    # Show workflow progress
                                    name, current, total = workflow_match.groups()
                                    print(f"    {timer} Running {name} [{current}/{total}]")
                                elif match := STEP_START.search(clean_line):
                                    current_step = match.group(1)
                                    current_step_output = []
                                    print(f"  {timer} {current_step}...")
                                elif match := STEP_SUCCESS.search(clean_line):
                                    step_name = match.group(1)
                                    print(f"  {timer} {step_name}... [OK]")
                                    completed_steps.append((step_name, True, []))
                                    current_step = None
                                elif match := STEP_FAILURE.search(clean_line):
                                    step_name = match.group(1)
                                    print(f"  {timer} {step_name}... [ERROR]")
                                    completed_steps.append((step_name, False, current_step_output.copy()))
                                    current_step = None

                                # Capture output for error context
                                if current_step and clean_line.strip():
                                    current_step_output.append(clean_line)
                                    if len(current_step_output) > 20:
                                        current_step_output.pop(0)
                        elif process.poll() is not None:
                            break
                    else:
                        break
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            subprocess.run(
                f"docker kill $(docker ps -q --filter ancestor={ACT_IMAGE}) 2>/dev/null",
                shell=True,
                capture_output=True,
            )
            log("\nTest cancelled")
            return_code = 130

        # Show error context for failed steps
        if not verbose and return_code != 130:
            for step_name, success, output in completed_steps:
                if not success and output:
                    log(f"\n  Error in {step_name}:")
                    for line in output[-5:]:
                        log(f"    {line}")

        # Split main log into per-workflow logs
        logs_dir = output_dir / "logs"
        if logs_dir.exists():
            subprocess.run(["sudo", "rm", "-rf", str(logs_dir)], capture_output=True)
        workflow_logs = split_log_by_workflow(log_file, logs_dir)

        # Fix ownership of output files (Docker runs as root)
        if output_dir.exists():
            subprocess.run(
                ["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(output_dir)],
                capture_output=True
            )

        # Report output
        screenshots_dir = output_dir / "screenshots"
        screenshot_files = list(screenshots_dir.glob("*.png")) if screenshots_dir.exists() else []
        results_file = output_dir / "results.json"

        if screenshot_files or results_file.exists() or log_file.exists():
            log(f"\nLog: {log_file}")
            if workflow_logs:
                log(f"Workflow logs: {workflow_logs}")
            if screenshot_files:
                log(f"Screenshots: {len(screenshot_files)}")

        if return_code != 0:
            return return_code
        return process.returncode or 0

    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)
