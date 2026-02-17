"""Windows Portable platform implementation for ComfyUI testing."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from ...common.base_platform import TestPlatform, TestPaths
from ...common.errors import DownloadError, SetupError
from .download import download_portable, get_latest_release_tag, extract_7z, get_cache_dir

if TYPE_CHECKING:
    from ...common.config import TestConfig


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
            if not line or line.startswith('#'):
                continue
            pattern = line.rstrip('/')
            gitignore_patterns.append(pattern)

    def ignore_func(directory: str, names: List[str]) -> List[str]:
        ignored = []
        try:
            rel_dir = Path(directory).relative_to(base_dir) if directory != str(base_dir) else Path('.')
        except ValueError:
            rel_dir = Path('.')

        for name in names:
            if name in always_ignore:
                ignored.append(name)
                continue

            if work_dir:
                full_path = Path(directory) / name
                try:
                    if full_path.resolve() == work_dir.resolve():
                        ignored.append(name)
                        continue
                except (OSError, ValueError):
                    pass

            rel_path = rel_dir / name
            for pattern in gitignore_patterns:
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel_path), pattern):
                    ignored.append(name)
                    break
                if pattern.endswith('/') and fnmatch.fnmatch(name, pattern[:-1]):
                    ignored.append(name)
                    break
                if '*' in pattern and fnmatch.fnmatch(name, pattern):
                    ignored.append(name)
                    break

        return ignored

    return ignore_func


# Local dev packages to build wheels for
LOCAL_DEV_PACKAGES = [
    ("comfy-env", Path.home() / "Desktop" / "utils" / "comfy-env"),
]


def _build_local_wheels(work_dir: Path, log) -> Optional[Path]:
    """Build wheels for local dev packages if they exist."""
    import sys
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
                    [sys.executable, "-m", "pip", "wheel", str(path), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                    capture_output=True,
                    check=True
                )
            except subprocess.CalledProcessError as e:
                log(f"  Warning: Failed to build {name} wheel: {e.stderr}")

    return wheel_dir if found_any else None


class WindowsPortablePlatform(TestPlatform):
    """Windows Portable platform implementation for ComfyUI testing."""

    def __init__(self, log_callback: Callable[[str], None] = None):
        super().__init__(log_callback)
        self._wheel_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return "windows_portable"

    @property
    def executable_suffix(self) -> str:
        return ".exe"

    def is_ci(self) -> bool:
        """Detect if running in CI environment."""
        return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"

    def is_gpu_mode(self) -> bool:
        """Detect if GPU mode is enabled."""
        return os.environ.get("COMFY_TEST_GPU", "0") not in ("0", "", "false", "no")

    def _uv_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run uv pip install with local wheels if available."""
        cmd = [str(python), "-m", "uv", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def _pip_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run pip install with local wheels if available."""
        cmd = [str(python), "-m", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """Set up ComfyUI Portable for testing on Windows."""
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        # Get portable version
        portable_config = config.windows_portable
        version = portable_config.comfyui_portable_version or "latest"

        if version == "latest":
            version = get_latest_release_tag(self._log)

        # Use persistent cache directory
        cache_dir = get_cache_dir()
        archive_path = cache_dir / f"ComfyUI_portable_{version}.7z"
        cached_extract_dir = cache_dir / f"ComfyUI_portable_{version}"

        # Download if not cached
        if not archive_path.exists():
            download_portable(version, archive_path, self._log)
        else:
            self._log(f"Using cached archive: {archive_path}")

        # Extract if not cached
        if not cached_extract_dir.exists() or not any(cached_extract_dir.iterdir()):
            self._log(f"Extracting to cache: {cached_extract_dir}")
            if cached_extract_dir.exists():
                shutil.rmtree(cached_extract_dir)
            extract_7z(archive_path, cached_extract_dir, self._log)
        else:
            self._log(f"Using cached extraction: {cached_extract_dir}")

        # Copy from cache to work directory
        import uuid
        portable_work_dir = Path.home() / "Desktop" / "portabletest"
        if portable_work_dir.exists():
            old_name = f"portabletest_old_{uuid.uuid4().hex[:8]}"
            old_path = Path.home() / "Desktop" / old_name
            self._log(f"Moving old folder to {old_name} (deleting in background)...")
            portable_work_dir.rename(old_path)
            subprocess.Popen(
                ["cmd", "/c", "rd", "/s", "/q", str(old_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._log(f"Copying from cache to: {portable_work_dir}")
        shutil.copytree(cached_extract_dir, portable_work_dir)
        extract_dir = portable_work_dir

        # Find ComfyUI directory
        comfyui_dir = self._find_comfyui_dir(extract_dir)
        if not comfyui_dir:
            raise SetupError(
                "Could not find ComfyUI directory in portable archive",
                f"Searched in: {extract_dir}"
            )

        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        # Find embedded Python
        python_embeded = extract_dir / "python_embeded"
        if not python_embeded.exists():
            for subdir in extract_dir.iterdir():
                if subdir.is_dir():
                    alt_python = subdir / "python_embeded"
                    if alt_python.exists():
                        python_embeded = alt_python
                        break

        if not python_embeded.exists():
            raise SetupError(
                "Could not find python_embeded in portable archive",
                f"Searched in: {extract_dir}"
            )

        python = python_embeded / "python.exe"

        # On Linux, make Windows executables executable
        import sys
        if sys.platform != "win32":
            import stat
            for exe in python_embeded.glob("*.exe"):
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Install uv into embedded Python
        self._log("Installing uv into embedded Python...")
        self._run_command(
            [str(python), "-m", "pip", "install", "uv"],
            cwd=comfyui_dir,
        )

        # Install ComfyUI's requirements
        comfyui_reqs = comfyui_dir / "requirements.txt"
        if comfyui_reqs.exists():
            self._log("Installing ComfyUI requirements...")
            self._uv_install(python, ["-r", str(comfyui_reqs)], comfyui_dir)

        return TestPaths(
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            python=python,
            custom_nodes_dir=custom_nodes_dir,
        )

    def install_node(self, paths: TestPaths, node_dir: Path, deps_installed: bool = False) -> None:
        """Install custom node into ComfyUI Portable."""
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name
        target_dir = paths.custom_nodes_dir / node_name

        if deps_installed:
            self._log("Skipping copy, requirements.txt, and install.py (--deps-installed)")
            return

        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(node_dir, target_dir, ignore=_gitignore_filter(node_dir, paths.work_dir))

        # Build local dev wheels
        wheel_dir = _build_local_wheels(paths.work_dir, self._log)
        self._wheel_dir = wheel_dir

        # Install local wheels first
        if wheel_dir and wheel_dir.exists():
            wheel_files = list(wheel_dir.glob("*.whl"))
            if wheel_files:
                self._log(f"Installing {len(wheel_files)} local wheel(s) (force-reinstall)...")
                for whl in wheel_files:
                    self._log(f"  Installing {whl.name}...")
                    self._uv_install(
                        paths.python,
                        [str(whl), "--force-reinstall", "--no-cache", "--no-deps"],
                        target_dir,
                    )

        # Install requirements.txt
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._pip_install(paths.python, ["-r", str(requirements_file)], target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log("Running install.py...")
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
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
    ) -> subprocess.Popen:
        """Start ComfyUI server using portable Python."""
        self._log(f"Starting ComfyUI server on port {port}...")

        cmd = [
            str(paths.python),
            "-s",
            str(paths.comfyui_dir / "main.py"),
            "--listen", "127.0.0.1",
            "--port", str(port),
            "--windows-standalone-build",
        ]

        if not self.is_gpu_mode():
            cmd.append("--cpu")

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
        """Clean up test environment."""
        self._log(f"Cleaning up {paths.work_dir}...")

        if paths.work_dir.exists():
            try:
                shutil.rmtree(paths.work_dir)
            except PermissionError:
                self._log("Warning: Could not fully clean up (files may be locked)")

    def _find_comfyui_dir(self, extract_dir: Path) -> Optional[Path]:
        """Find ComfyUI directory within extracted archive."""
        candidates = [
            extract_dir / "ComfyUI",
            extract_dir / "ComfyUI_windows_portable" / "ComfyUI",
        ]

        for subdir in extract_dir.iterdir():
            if subdir.is_dir():
                candidates.append(subdir / "ComfyUI")

        for candidate in candidates:
            if candidate.exists() and (candidate / "main.py").exists():
                return candidate

        return None

    def install_node_from_repo(self, paths: TestPaths, repo: str, name: str) -> None:
        """Install a custom node from a GitHub repository."""
        target_dir = paths.custom_nodes_dir / name
        git_url = f"https://github.com/{repo}.git"

        if target_dir.exists():
            self._log(f"  {name} already exists, skipping...")
            return

        self._log(f"  Cloning {repo}...")
        self._run_command(
            ["git", "clone", "--depth", "1", git_url, str(target_dir)],
            cwd=paths.custom_nodes_dir,
        )

        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log(f"  Installing {name} requirements...")
            self._pip_install(paths.python, ["-r", str(requirements_file)], target_dir)

        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log(f"  Running {name} install.py...")
            install_env = {"COMFY_ENV_CUDA_VERSION": "12.8"}
            self._run_command(
                [str(paths.python), str(install_py)],
                cwd=target_dir,
                env=install_env,
            )
