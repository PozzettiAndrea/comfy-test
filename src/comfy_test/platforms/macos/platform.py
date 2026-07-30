"""macOS platform implementation for ComfyUI testing."""

import os
import shutil
import sys
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..venv_server import VenvServerPlatform, COMFYUI_REPO
from ...common.base_platform import TestPaths
from ...common.config import resolve_torch_triple

if TYPE_CHECKING:
    from ...common.config import TestConfig


class MacOSPlatform(VenvServerPlatform):
    """macOS: stdlib venv + uv-in-venv bootstrap, no PyTorch index override
    (PyTorch ships no macOS `/whl/*` subindex), and MPS (never --cpu).

    Supports both Intel and Apple Silicon Macs.
    """

    _name = "macos"
    _pass_cpu_flag = False  # Apple Silicon MPS; ComfyUI auto-selects it without --cpu

    def _uv_install(self, python: Path, args: list, cwd: Path, env: Optional[dict] = None) -> None:
        """Install packages using uv pip (no torch index override on macOS)."""
        cmd = [str(python), "-m", "uv", "pip", "install"] + args
        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])
        cmd.extend(self._extra_index_args())
        self._run_command(cmd, cwd=cwd, env=env)

    def _install_reqs(self, requirements_file: Path, cwd: Path) -> None:
        # Node requirements install via plain uv (no PyTorch index routing).
        self._uv_install(self._venv_python, ["-r", str(requirements_file)], cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """Clone ComfyUI, create a stdlib venv, bootstrap uv, install torch + reqs."""
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        comfyui_dir = work_dir / "ComfyUI"
        self.set_extra_pip_indices(config)

        # Clone ComfyUI
        self._log(f"Cloning ComfyUI ({config.comfyui_version})...")
        if comfyui_dir.exists():
            shutil.rmtree(comfyui_dir)
        clone_args = ["git", "clone", "--depth", "1"]
        if config.comfyui_version != "latest":
            clone_args.extend(["--branch", config.comfyui_version])
        clone_args.extend([COMFYUI_REPO, str(comfyui_dir)])
        self._run_command(clone_args, cwd=work_dir)

        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        # Create virtual environment (stdlib venv), then bootstrap uv into it.
        self._log("Creating virtual environment...")
        venv_dir = work_dir / "venv"
        self._run_command([sys.executable, "-m", "venv", str(venv_dir)], cwd=work_dir)
        python = venv_dir / "bin" / "python"
        self._venv_python = python

        self._log("Installing uv into venv...")
        self._run_command([str(python), "-m", "pip", "install", "uv"], cwd=work_dir)

        # Install PyTorch (standard PyTorch works for both CPU and MPS on macOS).
        # No --index-url override: PyTorch publishes no `/whl/*` subindex with
        # macOS wheels, so default PyPI is authoritative and ships the MPS-capable
        # macosx_*_arm64 wheel. Pin the family to a known-good version (default
        # 2.10.0 from TORCH_TRIPLES) -- 2.12's only osx-arm64 wheel is tagged
        # macosx_14_0 (pixi targets macOS 13) and 2.11 has no +cu128; 2.10 ships
        # the full cu128 triple AND a macosx_11_0_arm64 wheel.
        env_torch = os.environ.get("COMFY_TEST_TORCH_VERSION", "").strip()
        torch_spec = env_torch or getattr(config, "torch_version", None)
        triple = resolve_torch_triple(torch_spec)
        self._log("Installing PyTorch...")
        if triple:
            t, tv, ta = triple
            self._log(f"Pinning torch family: torch=={t} torchvision=={tv} torchaudio=={ta}")
            self._uv_install(python, [f"torch=={t}", f"torchvision=={tv}", f"torchaudio=={ta}"], work_dir)
        else:
            self._uv_install(python, ["torch", "torchvision", "torchaudio"], work_dir)

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
