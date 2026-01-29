"""Windows platform implementation for ComfyUI testing."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from .base import TestPlatform, TestPaths

if TYPE_CHECKING:
    from ..config import TestConfig


COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# Local dev packages to build wheels for
LOCAL_DEV_PACKAGES = [
    ("comfy-env", Path.home() / "Desktop" / "utils" / "comfy-env"),
    ("comfy-test", Path.home() / "Desktop" / "utils" / "comfy-test"),
]


def _build_local_wheels(work_dir: Path, log: Callable[[str], None]) -> Optional[Path]:
    """Build wheels for local dev packages if they exist.

    Returns the wheel directory path, or None if no local packages found.
    """
    wheel_dir = work_dir / "local_wheels"

    found_any = False
    for name, path in LOCAL_DEV_PACKAGES:
        if path.exists():
            if not found_any:
                wheel_dir.mkdir(parents=True, exist_ok=True)
                found_any = True

            log(f"Building {name} wheel...")
            try:
                subprocess.run(
                    ["pip", "wheel", str(path), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                    capture_output=True,
                    check=True
                )
            except subprocess.CalledProcessError as e:
                log(f"  Warning: Failed to build {name} wheel: {e.stderr}")

    return wheel_dir if found_any else None


def _gitignore_filter(base_dir: Path, work_dir: Path = None):
    """Create a shutil.copytree ignore function based on .gitignore patterns."""
    import fnmatch
    from typing import List

    # Always ignore these (essential for clean copy)
    always_ignore = {'.git', '__pycache__', '.comfy-test',
                     '.comfy-test-logs', '.venv', 'venv', 'node_modules'}

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
        try:
            rel_dir = Path(directory).relative_to(base_dir) if directory != str(base_dir) else Path('.')
        except ValueError:
            rel_dir = Path('.')

        for name in names:
            # Always ignore these
            if name in always_ignore:
                ignored.append(name)
                continue

            # Skip the work_dir if it's inside the source
            if work_dir:
                full_path = Path(directory) / name
                try:
                    if full_path.resolve() == work_dir.resolve():
                        ignored.append(name)
                        continue
                except (OSError, ValueError):
                    pass

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
                # Handle patterns starting with * like _env_*
                if '*' in pattern and fnmatch.fnmatch(name, pattern):
                    ignored.append(name)
                    break

        return ignored

    return ignore_func


class WindowsTestPlatform(TestPlatform):
    """Windows platform implementation for ComfyUI testing.

    Creates a venv for isolated testing (avoids admin requirements).
    Uses local wheels for dev packages (comfy-env, comfy-test) when available.
    """

    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):
        super().__init__(log_callback)
        self._wheel_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return "windows"

    @property
    def executable_suffix(self) -> str:
        return ".exe"

    def _uv_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run uv pip install with local wheels if available."""
        cmd = [str(python), "-m", "uv", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def _get_ci_python(self, paths_python: Path) -> Path:
        """Get the correct Python for CI environments.

        In GitHub Actions, sys.executable points to system Python but deps
        are in the venv at ~/venv/venv. Use venv Python if available.
        """
        if os.environ.get("GITHUB_ACTIONS"):
            venv_python = Path.home() / "venv" / "venv" / "Scripts" / "python.exe"
            if venv_python.exists():
                return venv_python
        return paths_python

    def _pip_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run pip install with local wheels if available (matches user experience)."""
        cmd = [str(python), "-m", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI for testing on Windows.

        1. Build wheels for local dev packages
        2. Clone ComfyUI from GitHub
        3. Create a virtual environment
        4. Install PyTorch (CPU) and requirements
        """
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        # Build wheels for local dev packages first
        self._wheel_dir = _build_local_wheels(work_dir, self._log)
        if self._wheel_dir:
            self._log(f"Local wheels built in: {self._wheel_dir}")

        comfyui_dir = work_dir / "ComfyUI"

        # Clone ComfyUI
        self._log(f"Cloning ComfyUI ({config.comfyui_version})...")
        if comfyui_dir.exists():
            shutil.rmtree(comfyui_dir)

        clone_args = ["git", "clone", "--depth", "1"]
        if config.comfyui_version != "latest":
            clone_args.extend(["--branch", config.comfyui_version])
        clone_args.extend([COMFYUI_REPO, str(comfyui_dir)])

        self._run_command(clone_args, cwd=work_dir)

        # Create custom_nodes directory
        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        # Create virtual environment
        venv_dir = work_dir / "venv"
        self._log("Creating virtual environment...")
        self._run_command(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=work_dir,
        )

        # Use venv Python
        python = venv_dir / "Scripts" / "python.exe"

        # Install uv into venv first
        self._log("Installing uv into venv...")
        self._run_command(
            [str(python), "-m", "pip", "install", "uv"],
            cwd=work_dir,
        )

        # Install PyTorch (CPU)
        self._log("Installing PyTorch (CPU)...")
        self._uv_install(python, [
            "torch==2.8.0", "torchvision", "torchaudio",
            "--index-url", PYTORCH_CPU_INDEX
        ], cwd=work_dir)

        # Install ComfyUI requirements
        self._log("Installing ComfyUI requirements...")
        requirements_file = comfyui_dir / "requirements.txt"
        if requirements_file.exists():
            self._uv_install(python, ["-r", str(requirements_file)], cwd=work_dir)

        return TestPaths(
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            python=python,
            custom_nodes_dir=custom_nodes_dir,
        )

    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """
        Install custom node into ComfyUI.

        On Windows, we copy instead of symlink to avoid permission issues.

        1. Copy to custom_nodes/ (respecting .gitignore)
        2. Install requirements.txt if present (with local wheels)
        3. Run install.py if present (with COMFY_LOCAL_WHEELS env var)
        """
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name

        target_dir = paths.custom_nodes_dir / node_name

        # Copy node directory
        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(node_dir, target_dir, ignore=_gitignore_filter(node_dir, paths.work_dir))

        # Install requirements.txt first (install.py may depend on these)
        # Uses pip (not uv) to match user experience
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            python = self._get_ci_python(paths.python)
            self._pip_install(python, ["-r", str(requirements_file)], cwd=target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log("Running install.py...")
            python = self._get_ci_python(paths.python)
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
            # Pass wheel dir so pixi/pip inside install.py can use local wheels
            if self._wheel_dir and self._wheel_dir.exists():
                install_env["COMFY_LOCAL_WHEELS"] = str(self._wheel_dir)
            self._run_command(
                [str(python), str(install_py)],
                cwd=target_dir,
                env=install_env,
            )

    def start_server(
        self,
        paths: TestPaths,
        config: "TestConfig",
        port: int = 8188,
        extra_env: Optional[dict] = None,
    ) -> subprocess.Popen:
        """Start ComfyUI server on Windows."""
        self._log(f"Starting ComfyUI server on port {port}...")

        python = self._get_ci_python(paths.python)
        cmd = [
            str(python),
            "-u",  # Unbuffered stdout/stderr for immediate output
            str(paths.comfyui_dir / "main.py"),
            "--listen", "127.0.0.1",
            "--port", str(port),
            "--verbose", "DEBUG",  # Enable detailed logging
            "--log-stdout",        # Send logs to stdout for capture
        ]

        # Use CPU mode unless GPU mode is explicitly enabled
        gpu_mode = os.environ.get("COMFY_TEST_GPU")
        if not gpu_mode:
            cmd.append("--cpu")

        # Set environment
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        process = subprocess.Popen(
            cmd,
            cwd=paths.comfyui_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        return process

    def cleanup(self, paths: TestPaths) -> None:
        """Clean up test environment on Windows."""
        self._log(f"Cleaning up {paths.work_dir}...")

        if paths.work_dir.exists():
            try:
                shutil.rmtree(paths.work_dir)
            except PermissionError:
                self._log("Warning: Could not fully clean up (files may be locked)")

    def install_node_from_repo(self, paths: TestPaths, repo: str, name: str) -> None:
        """
        Install a custom node from a GitHub repository.

        1. Git clone into custom_nodes/
        2. Install requirements.txt if present (with local wheels)
        3. Run install.py if present
        """
        target_dir = paths.custom_nodes_dir / name
        git_url = f"https://github.com/{repo}.git"

        # Skip if already installed
        if target_dir.exists():
            self._log(f"  {name} already exists, skipping...")
            return

        # Clone the repo
        self._log(f"  Cloning {repo}...")
        self._run_command(
            ["git", "clone", "--depth", "1", git_url, str(target_dir)],
            cwd=paths.custom_nodes_dir,
        )

        # Install requirements.txt first (with local wheels)
        # Uses pip (not uv) to match user experience
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log(f"  Installing {name} requirements...")
            python = self._get_ci_python(paths.python)
            self._pip_install(python, ["-r", str(requirements_file)], cwd=target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log(f"  Running {name} install.py...")
            python = self._get_ci_python(paths.python)
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
            if self._wheel_dir and self._wheel_dir.exists():
                install_env["COMFY_LOCAL_WHEELS"] = str(self._wheel_dir)
            self._run_command(
                [str(python), str(install_py)],
                cwd=target_dir,
                env=install_env,
            )
