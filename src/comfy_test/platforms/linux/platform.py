"""Linux platform implementation for ComfyUI testing."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from ...common.base_platform import TestPlatform, TestPaths

if TYPE_CHECKING:
    from ...common.config import TestConfig


COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
PYTORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
PYPI_INDEX = "https://pypi.org/simple"


class LinuxPlatform(TestPlatform):
    """Linux platform implementation for ComfyUI testing."""

    def __init__(self, log_callback=None):
        super().__init__(log_callback)
        self._venv_python: Optional[Path] = None

    @property
    def name(self) -> str:
        return "linux"

    @property
    def executable_suffix(self) -> str:
        return ""

    def is_ci(self) -> bool:
        """Detect if running in CI environment."""
        return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"

    def is_gpu_mode(self) -> bool:
        """Detect if GPU mode is enabled."""
        return os.environ.get("COMFY_TEST_GPU", "0") not in ("0", "", "false", "no")

    def _pip_install_requirements(self, requirements_file: Path, cwd: Path) -> None:
        """Install requirements with proper PyTorch index for GPU/CPU mode."""
        # Use venv python if available, otherwise fallback to system
        if self._venv_python:
            cmd = ["uv", "pip", "install", "--python", str(self._venv_python)]
        else:
            cmd = ["uv", "pip", "install", "--system"]

        # Use local wheels if available (for local testing with ct test)
        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])

        if self.is_gpu_mode():
            # GPU mode: prioritize CUDA index, fallback to PyPI
            cmd.extend(["--index-url", PYTORCH_CUDA_INDEX])
            cmd.extend(["--extra-index-url", PYPI_INDEX])
        cmd.extend(["-r", str(requirements_file)])

        self._run_command(cmd, cwd=cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI for testing on Linux.

        1. Create venv in work directory
        2. Clone ComfyUI from GitHub
        3. Install requirements to venv
        4. Install PyTorch (CPU or CUDA)
        """
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        comfyui_dir = work_dir / "ComfyUI"
        venv_dir = work_dir / ".venv"

        # Create venv (isolated from system Python)
        self._log("Creating virtual environment...")
        self._run_command(["uv", "venv", str(venv_dir), "--python", "3.10"], cwd=work_dir)
        python = venv_dir / "bin" / "python"
        self._venv_python = python

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

        # Install ComfyUI requirements (uses CUDA index in GPU mode)
        self._log("Installing ComfyUI requirements...")
        requirements_file = comfyui_dir / "requirements.txt"
        if requirements_file.exists():
            self._pip_install_requirements(requirements_file, work_dir)

        # Install local dev packages if available (so install.py uses local version)
        utils_dir = Path.home() / "utils"
        for pkg in ["comfy-env", "comfy-test", "comfy-3d-viewers"]:
            pkg_path = utils_dir / pkg
            if pkg_path.exists():
                self._log(f"Installing local {pkg} (editable)...")
                self._run_command(
                    ["uv", "pip", "install", "-e", str(pkg_path), "--python", str(python)],
                    cwd=work_dir,
                )

        return TestPaths(
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            python=python,
            custom_nodes_dir=custom_nodes_dir,
        )

    def install_node(self, paths: TestPaths, node_dir: Path, deps_installed: bool = False) -> None:
        """
        Install custom node into ComfyUI.

        1. Symlink to custom_nodes/
        2. Install requirements.txt if present - unless deps_installed
        3. Run install.py if present - unless deps_installed
        """
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name

        target_dir = paths.custom_nodes_dir / node_name

        if deps_installed:
            self._log("Skipping copy, requirements.txt, and install.py (--deps-installed)")
            return

        # Copy node (not symlink) for full isolation
        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            shutil.rmtree(target_dir)

        # Parse .gitignore patterns
        gitignore_patterns = set()
        gitignore_path = node_dir / ".gitignore"
        if gitignore_path.exists():
            for line in gitignore_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    # Normalize pattern (remove trailing /)
                    pattern = line.rstrip("/")
                    gitignore_patterns.add(pattern)

        # Always ignore .git
        gitignore_patterns.add(".git")

        def ignore_patterns(directory, files):
            """Ignore files matching .gitignore patterns."""
            ignored = []
            dir_path = Path(directory)
            for f in files:
                # Check exact match
                if f in gitignore_patterns:
                    ignored.append(f)
                    continue
                # Check wildcard patterns (simple glob)
                for pattern in gitignore_patterns:
                    if pattern.startswith("*") and f.endswith(pattern[1:]):
                        ignored.append(f)
                        break
                    elif pattern.startswith("_") and f.startswith(pattern.rstrip("*")):
                        # Handle _env_* style patterns
                        ignored.append(f)
                        break
            return ignored

        shutil.copytree(node_dir, target_dir, ignore=ignore_patterns)

        # Install requirements.txt first (install.py may depend on these)
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._pip_install_requirements(requirements_file, target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log("Running install.py...")
            install_env = {
                "COMFY_ENV_CUDA_VERSION": "12.8",
                "COMFY_ENV_CACHE_DIR": str(paths.work_dir / ".comfy-env"),
            }
            self._run_command(
                [str(paths.python), str(install_py)],
                cwd=target_dir,
                env=install_env,
            )

    def start_server(
        self,
        paths: TestPaths,
        config: "TestConfig",
        port: int = 8188,
        extra_env: Optional[dict] = None,
        extra_args: Optional[list[str]] = None,
    ) -> subprocess.Popen:
        """Start ComfyUI server on Linux."""
        self._log(f"Starting ComfyUI server on port {port}...")

        cmd = [
            str(paths.python),
            str(paths.comfyui_dir / "main.py"),
            "--listen", "127.0.0.1",
            "--port", str(port),
        ]

        # Use CPU mode unless GPU mode is explicitly enabled
        if not self.is_gpu_mode():
            cmd.append("--cpu")

        if extra_args:
            cmd.extend(extra_args)

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
        """Clean up test environment on Linux."""
        self._log(f"Cleaning up {paths.work_dir}...")

        if paths.work_dir.exists():
            shutil.rmtree(paths.work_dir, ignore_errors=True)

    def install_node_from_repo(self, paths: TestPaths, repo: str, name: str) -> None:
        """
        Install a custom node from a GitHub repository.

        1. Git clone into custom_nodes/
        2. Install requirements.txt if present
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

        # Install requirements.txt first
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log(f"  Installing {name} requirements...")
            self._pip_install_requirements(requirements_file, target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log(f"  Running {name} install.py...")
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
            self._run_command(
                [str(paths.python), str(install_py)],
                cwd=target_dir,
                env=install_env,
            )
