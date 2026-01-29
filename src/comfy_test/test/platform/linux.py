"""Linux platform implementation for ComfyUI testing."""

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
PYTORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
PYPI_INDEX = "https://pypi.org/simple"


class LinuxTestPlatform(TestPlatform):
    """Linux platform implementation for ComfyUI testing."""

    @property
    def name(self) -> str:
        return "linux"

    @property
    def executable_suffix(self) -> str:
        return ""

    def _pip_install_requirements(self, requirements_file: Path, cwd: Path) -> None:
        """Install requirements with proper PyTorch index for GPU/CPU mode."""
        gpu_mode = os.environ.get("COMFY_TEST_GPU")

        cmd = ["uv", "pip", "install", "--system"]

        # Use local wheels if available (for local testing with ct test)
        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])

        if gpu_mode:
            # GPU mode: prioritize CUDA index, fallback to PyPI
            cmd.extend(["--index-url", PYTORCH_CUDA_INDEX])
            cmd.extend(["--extra-index-url", PYPI_INDEX])
        cmd.extend(["-r", str(requirements_file)])

        self._run_command(cmd, cwd=cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI for testing on Linux.

        1. Clone ComfyUI from GitHub
        2. Install requirements to system Python
        3. Install PyTorch (CPU or CUDA)
        """
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

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

        # Use system Python
        python = Path(sys.executable)

        # Install ComfyUI requirements (uses CUDA index in GPU mode)
        self._log("Installing ComfyUI requirements...")
        requirements_file = comfyui_dir / "requirements.txt"
        if requirements_file.exists():
            self._pip_install_requirements(requirements_file, work_dir)

        return TestPaths(
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            python=python,
            custom_nodes_dir=custom_nodes_dir,
        )

    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """
        Install custom node into ComfyUI.

        1. Symlink to custom_nodes/
        2. Install requirements.txt if present
        3. Run install.py if present
        """
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name

        target_dir = paths.custom_nodes_dir / node_name

        # Create symlink
        self._log(f"Linking {node_name} to custom_nodes/...")
        if target_dir.exists():
            if target_dir.is_symlink():
                target_dir.unlink()
            else:
                shutil.rmtree(target_dir)

        target_dir.symlink_to(node_dir)

        # Install requirements.txt first (install.py may depend on these)
        requirements_file = node_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._pip_install_requirements(requirements_file, node_dir)

        # Run install.py if present
        install_py = node_dir / "install.py"
        if install_py.exists():
            self._log("Running install.py...")
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
            self._run_command(
                [str(paths.python), str(install_py)],
                cwd=node_dir,
                env=install_env,
            )

    def start_server(
        self,
        paths: TestPaths,
        config: "TestConfig",
        port: int = 8188,
        extra_env: Optional[dict] = None,
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
