"""Shared venv-based server platform for Linux / Windows / macOS.

These three OS targets are the same execution model -- create a virtualenv, pin
torch, clone + install ComfyUI, install the node, boot `main.py` -- and differ
only in a handful of knobs. This base holds the whole flow (verbatim from the
old LinuxPlatform); each OS is a thin subclass that overrides only what really
differs:

  - Linux:   just the knobs (bin/, no suffix).
  - Windows: knobs (Scripts/, .exe) + `_log_requirements_file` + a cleanup that
             tolerates locked files.
  - macOS:   a different venv bootstrap (stdlib venv + uv-in-venv, no torch
             index url, MPS not CPU), so it overrides setup_comfyui / _install_reqs
             and sets `_pass_cpu_flag = False`.

windows_portable (embedded python, no venv) is intentionally NOT here.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple, TYPE_CHECKING

from ..common.base_platform import TestPlatform, TestPaths
from ..common.config import resolve_torch_triple

if TYPE_CHECKING:
    from ..common.config import TestConfig


COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
# CUDA torch index lives in backends/cuda.py (CudaBackend.torch_index); this
# file holds only the backend-neutral CPU index and dispatches for the rest.
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"


class VenvServerPlatform(TestPlatform):
    """Venv + ComfyUI-server test platform, parameterized per OS by class attrs."""

    # --- per-OS knobs (subclasses override) ---
    _name: str = "venv"
    _exe_suffix: str = ""
    _venv_dirname: str = ".venv"
    _venv_bindir: str = "bin"            # "bin" (unix) | "Scripts" (windows)
    _venv_python_name: str = "python"    # "python" | "python.exe"
    _pass_cpu_flag: bool = True          # macOS uses MPS -> False (no --cpu)

    def __init__(self, log_callback=None):
        super().__init__(log_callback)
        self._venv_python: Optional[Path] = None
        # Pinned torch triple for the current run, set by setup_comfyui from
        # the TestConfig. None = no pin (use whatever uv resolves freely).
        self._torch_triple: Optional[Tuple[str, str, str]] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def executable_suffix(self) -> str:
        return self._exe_suffix

    def is_ci(self) -> bool:
        """Detect if running in CI environment."""
        return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"

    def is_cuda_mode(self) -> bool:
        """Detect if the CUDA accelerator is enabled."""
        return os.environ.get("COMFY_TEST_CUDA", "0") not in ("0", "", "false", "no")

    # --- override hooks ------------------------------------------------------

    def _log_requirements_file(self, requirements_file: Path) -> None:
        """Print a requirements.txt so pinned versions are visible in the log.
        No-op by default; Windows overrides to actually print (parity with the
        old WindowsPlatform)."""
        return

    def _install_reqs(self, requirements_file: Path, cwd: Path) -> None:
        """Install a requirements.txt for a node. Base routes through the
        PyTorch-index-aware pip install; macOS overrides to plain uv."""
        self._pip_install_requirements(requirements_file, cwd)

    # --- torch / requirements (index-routed; linux+windows) ------------------

    def _pip_install_torch_family(self, cwd: Path) -> None:
        """Install the pinned (torch, torchvision, torchaudio) triple before any
        other requirements. Skips if no triple is configured (free resolution)."""
        if not self._torch_triple:
            return
        if not self._venv_python:
            return
        t, tv, ta = self._torch_triple
        # Always route torch through the PyTorch wheel server (cu128 for CUDA,
        # cpu for CPU lanes) -- never leave it to default PyPI, where the torch
        # wheel is CUDA-built (~800 MB on Linux) / CPU-only (Windows), so a
        # fall-through silently produces the wrong torch.
        # unsafe-best-match (NOT first-index) is required: uv parses `==2.10.0`
        # as `>=2.10.0, <2.10.0+`, which excludes local-versioned wheels like
        # `2.10.0+cu128`. With first-index uv stops at PyPI and never falls
        # through. unsafe-best-match picks the highest-versioned candidate across
        # all indexes (the +cu128 / +cpu local-variant wins). "unsafe" only
        # matters across mutually-untrusted indexes; PyTorch + PyPI are trusted.
        cmd = ["uv", "pip", "install", "--python", str(self._venv_python)]
        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])
        from ..backends import active_backend
        torch_index = active_backend().torch_index() if self.is_cuda_mode() else PYTORCH_CPU_INDEX
        cmd.extend(["--index-url", torch_index])
        cmd.extend(["--extra-index-url", PYPI_INDEX])
        cmd.extend(self._extra_index_args())
        cmd.extend(["--index-strategy", "unsafe-best-match"])
        cmd.extend([f"torch=={t}", f"torchvision=={tv}", f"torchaudio=={ta}"])
        self._log(f"Pinning torch family from {torch_index}: torch=={t} torchvision=={tv} torchaudio=={ta}")
        self._run_command(cmd, cwd=cwd)

    def _pip_install_requirements(self, requirements_file: Path, cwd: Path) -> None:
        """Install requirements with the proper PyTorch index for CUDA/CPU mode."""
        if self._venv_python:
            cmd = ["uv", "pip", "install", "--python", str(self._venv_python)]
        else:
            cmd = ["uv", "pip", "install", "--system"]

        local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
        if local_wheels and Path(local_wheels).exists():
            cmd.extend(["--find-links", local_wheels])

        # Prioritize the PyTorch wheel server so torch ecosystem deps resolve
        # from the same source the explicit pin came from. unsafe-best-match for
        # the same local-version-wheel reason as _pip_install_torch_family.
        from ..backends import active_backend
        torch_index = active_backend().torch_index() if self.is_cuda_mode() else PYTORCH_CPU_INDEX
        cmd.extend(["--index-url", torch_index])
        cmd.extend(["--extra-index-url", PYPI_INDEX])
        cmd.extend(self._extra_index_args())
        cmd.extend(["--index-strategy", "unsafe-best-match"])
        cmd.extend(["-r", str(requirements_file)])

        self._run_command(cmd, cwd=cwd)

    # --- lifecycle -----------------------------------------------------------

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """Create the venv, clone ComfyUI, install the pinned torch + requirements."""
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        comfyui_dir = work_dir / "ComfyUI"
        venv_dir = work_dir / self._venv_dirname

        # Create venv (isolated from system Python)
        self._log(f"Creating virtual environment at {venv_dir}...")
        self._run_command(["uv", "venv", str(venv_dir), "--python", config.python_version], cwd=work_dir)
        python = venv_dir / self._venv_bindir / self._venv_python_name
        self._venv_python = python

        # Resolve the pinned torch triple from the config (or env override).
        env_torch = os.environ.get("COMFY_TEST_TORCH_VERSION", "").strip()
        torch_spec = env_torch or getattr(config, "torch_version", None)
        self._torch_triple = resolve_torch_triple(torch_spec)
        self.set_extra_pip_indices(config)
        if self._torch_triple:
            t, tv, ta = self._torch_triple
            self._log(f"torch_version={torch_spec!r} -> pinning torch=={t} torchvision=={tv} torchaudio=={ta}")
        else:
            self._log(f"torch_version={torch_spec!r} -> no pin (uv will resolve freely)")

        # Clone ComfyUI
        self._log(f"Cloning ComfyUI ({config.comfyui_version}) to {comfyui_dir}...")
        if comfyui_dir.exists():
            shutil.rmtree(comfyui_dir)

        clone_args = ["git", "clone", "--depth", "1"]
        if config.comfyui_version != "latest":
            clone_args.extend(["--branch", config.comfyui_version])
        clone_args.extend([COMFYUI_REPO, str(comfyui_dir)])
        self._run_command(clone_args, cwd=work_dir)

        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        # Install the pinned torch family FIRST so the subsequent requirements
        # install sees it satisfied and doesn't try to upgrade it (which produced
        # the 2.12+cu130 vs 2.11+cu128 torchaudio skew).
        self._pip_install_torch_family(work_dir)

        self._log(f"Installing ComfyUI requirements into {venv_dir}...")
        requirements_file = comfyui_dir / "requirements.txt"
        if requirements_file.exists():
            self._log_requirements_file(requirements_file)
            self._pip_install_requirements(requirements_file, work_dir)

        # Install local dev packages if available (so install.py uses local version)
        utils_dir = Path(os.environ["COMFY_TEST_LOCAL_UTILS"]) if os.environ.get("COMFY_TEST_LOCAL_UTILS") else None
        if utils_dir:
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

    def _copy_node_tree(self, node_dir: Path, target_dir: Path) -> None:
        """Copy a node into custom_nodes/, honoring .gitignore (always dropping .git)."""
        gitignore_patterns = set()
        gitignore_path = node_dir / ".gitignore"
        if gitignore_path.exists():
            for line in gitignore_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    gitignore_patterns.add(line.rstrip("/"))
        gitignore_patterns.add(".git")

        def ignore_patterns(directory, files):
            ignored = []
            for f in files:
                if f in gitignore_patterns:
                    ignored.append(f)
                    continue
                for pattern in gitignore_patterns:
                    if pattern.startswith("*") and f.endswith(pattern[1:]):
                        ignored.append(f)
                        break
                    elif pattern.startswith("_") and f.startswith(pattern.rstrip("*")):
                        ignored.append(f)
                        break
            return ignored

        shutil.copytree(node_dir, target_dir, ignore=ignore_patterns)

    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """Copy the node into custom_nodes/, install its requirements, run install.py."""
        # Take .name BEFORE .resolve(): inside a Windows container, bind-mount
        # filters return the resolved path case-folded to lowercase, breaking
        # later case-sensitive imports. Harmless on Linux/macOS.
        node_name = node_dir.name
        node_dir = Path(node_dir).resolve()
        target_dir = paths.custom_nodes_dir / node_name

        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            if target_dir.is_symlink():
                target_dir.unlink()
            else:
                shutil.rmtree(target_dir)
        self._copy_node_tree(node_dir, target_dir)

        # Install requirements.txt first (install.py may depend on these)
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._log_requirements_file(requirements_file)
            self._install_reqs(requirements_file, target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log("\nRunning install.py...")
            install_env = {
                "COMFY_ENV_CUDA_VERSION": "12.8",
                "COMFY_ENV_CACHE_DIR": str(paths.work_dir / ".comfy-env"),
            }
            result = self._run_command(
                [str(paths.python), str(install_py)],
                cwd=target_dir,
                env=install_env,
                check=False,
                verbose=True,  # install.py prints structured progress; stream live
            )
            if result.returncode != 0:
                self._log(f"Warning: install.py failed (exit code {result.returncode}), continuing...")
                if result.stderr:
                    for line in result.stderr.strip().splitlines()[-20:]:
                        self._log(f"  [!] {line}")

    def start_server(
        self,
        paths: TestPaths,
        config: "TestConfig",
        port: int = 8188,
        extra_env: Optional[dict] = None,
        extra_args: Optional[list[str]] = None,
    ) -> subprocess.Popen:
        """Start the ComfyUI server."""
        self._log(f"Starting ComfyUI server on port {port}...")

        cmd = [
            str(paths.python),
            str(paths.comfyui_dir / "main.py"),
            "--listen", "127.0.0.1",
            "--port", str(port),
        ]
        # CPU mode unless CUDA is enabled. macOS opts out (_pass_cpu_flag=False)
        # so ComfyUI auto-selects MPS on Apple Silicon.
        if self._pass_cpu_flag and not self.is_cuda_mode():
            cmd.append("--cpu")
        if extra_args:
            cmd.extend(extra_args)

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        return subprocess.Popen(
            cmd,
            cwd=paths.comfyui_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def cleanup(self, paths: TestPaths) -> None:
        """Clean up the test working directory."""
        self._log(f"Cleaning up {paths.work_dir}...")
        if paths.work_dir.exists():
            shutil.rmtree(paths.work_dir, ignore_errors=True)

    def install_node_from_repo(self, paths: TestPaths, repo: str, name: str) -> None:
        """Clone a node dependency from GitHub, install its requirements + install.py."""
        target_dir = paths.custom_nodes_dir / name
        # authenticated_github_url embeds NODE_PAT/GH_TOKEN/GITHUB_TOKEN when set,
        # so private node deps clone the same way public ones do.
        from ..cli._git_auth import authenticated_github_url, git_env, tokens_to_redact
        git_url = authenticated_github_url(repo)

        if target_dir.exists():
            self._log(f"  {name} already exists, skipping...")
            return

        # redact= masks the PAT in the logged command and captured output so it
        # never reaches session.log (GitHub Push Protection blocks otherwise).
        self._log(f"  Cloning {repo}...")
        self._run_command(
            ["git", "clone", "--depth", "1", git_url, str(target_dir)],
            cwd=paths.custom_nodes_dir,
            env=git_env(),
            redact=tokens_to_redact(),
        )

        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log(f"  Installing {name} requirements...")
            self._install_reqs(requirements_file, target_dir)

        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log(f"  Running {name} install.py...")
            result = self._run_command(
                [str(paths.python), str(install_py)],
                cwd=target_dir,
                env={"COMFY_ENV_CUDA_VERSION": "12.8"},
                check=False,
            )
            if result.returncode != 0:
                self._log(f"Warning: {name} install.py failed (exit code {result.returncode}), continuing...")
                if result.stderr:
                    for line in result.stderr.strip().splitlines()[-20:]:
                        self._log(f"  [!] {line}")
