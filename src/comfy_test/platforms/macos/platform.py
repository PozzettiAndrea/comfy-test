"""macOS platform implementation for ComfyUI testing."""

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


class MacOSPlatform(TestPlatform):
    """macOS platform implementation for ComfyUI testing.

    Uses a virtual environment for isolation and safety.
    Supports both Intel and Apple Silicon Macs.
    """

    @property
    def name(self) -> str:
        return "macos"

    @property
    def executable_suffix(self) -> str:
        return ""

    def is_ci(self) -> bool:
        """Detect if running in CI environment."""
        return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"

    def is_gpu_mode(self) -> bool:
        """Detect if GPU mode is enabled (MPS on Apple Silicon)."""
        return bool(os.environ.get("COMFY_TEST_GPU"))

    def _uv_install(self, python: Path, args: list, cwd: Path, env: Optional[dict] = None) -> None:
        """Install packages using uv pip."""
        cmd = [str(python), "-m", "uv", "pip", "install"] + args

        # Use local wheels if available
        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])

        self._run_command(cmd, cwd=cwd, env=env)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI for testing on macOS.

        1. Clone ComfyUI from GitHub
        2. Create virtual environment
        3. Install requirements
        4. Install PyTorch (CPU or MPS for Apple Silicon)
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

        # Create virtual environment
        self._log("Creating virtual environment...")
        venv_dir = work_dir / "venv"
        self._run_command(
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=work_dir,
        )

        # Get venv Python path
        python = venv_dir / "bin" / "python"

        # Install uv into venv
        self._log("Installing uv into venv...")
        self._run_command(
            [str(python), "-m", "pip", "install", "uv"],
            cwd=work_dir,
        )

        # Install PyTorch (standard PyTorch works for both CPU and MPS on macOS)
        self._log("Installing PyTorch...")
        self._uv_install(python, ["torch", "torchvision", "torchaudio"], work_dir)

        # Install ComfyUI requirements
        self._log("Installing ComfyUI requirements...")
        requirements_file = comfyui_dir / "requirements.txt"
        if requirements_file.exists():
            self._uv_install(python, ["-r", str(requirements_file)], work_dir)

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
            self._log("Skipping symlink, requirements.txt, and install.py (--deps-installed)")
            return

        # Create symlink (macOS supports symlinks natively)
        self._log(f"Linking {node_name} to custom_nodes/...")
        if target_dir.exists():
            if target_dir.is_symlink():
                target_dir.unlink()
            else:
                shutil.rmtree(target_dir)

        target_dir.symlink_to(node_dir)

        # Install requirements.txt first
        requirements_file = node_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._uv_install(paths.python, ["-r", str(requirements_file)], node_dir)

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
        """Start ComfyUI server on macOS."""
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
        """Clean up test environment on macOS."""
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
            self._uv_install(paths.python, ["-r", str(requirements_file)], target_dir)

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
