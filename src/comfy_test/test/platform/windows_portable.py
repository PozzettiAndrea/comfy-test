"""Windows Portable platform implementation for ComfyUI testing."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

import requests

from .base import TestPlatform, TestPaths
from ...errors import DownloadError, SetupError

if TYPE_CHECKING:
    from ..config import TestConfig


# ComfyUI portable release URLs
PORTABLE_RELEASE_URL = "https://github.com/comfyanonymous/ComfyUI/releases/download/{version}/ComfyUI_windows_portable_nvidia.7z"
PORTABLE_LATEST_URL = "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z"
PORTABLE_LATEST_API = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"

# Local dev packages to build wheels for (only comfy-env needed for junction fix)
LOCAL_DEV_PACKAGES = [
    ("comfy-env", Path.home() / "Desktop" / "utils" / "comfy-env"),
]


def _build_local_wheels(work_dir: Path, log) -> Optional[Path]:
    """Build wheels for local dev packages if they exist.

    Uses system Python (not embedded) because it has hatchling installed.
    Returns the wheel directory path, or None if no local packages found.
    """
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
                # Use system Python (has hatchling) not embedded Python
                subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", str(path), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
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


def _get_cache_dir() -> Path:
    """Get persistent cache directory for portable downloads."""
    cache_dir = Path.home() / ".comfy-test" / "cache" / "portable"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class WindowsPortableTestPlatform(TestPlatform):
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

    def _uv_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run uv pip install with local wheels if available."""
        cmd = [str(python), "-m", "uv", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def _pip_install(self, python: Path, args: list, cwd: Path) -> None:
        """Run pip install with local wheels if available (matches user experience)."""
        cmd = [str(python), "-m", "pip", "install"]
        if self._wheel_dir and self._wheel_dir.exists():
            cmd.extend(["--find-links", str(self._wheel_dir)])
        cmd.extend(args)
        self._run_command(cmd, cwd=cwd)

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI Portable for testing on Windows.

        1. Determine version to download
        2. Download 7z archive from GitHub releases (cached)
        3. Extract with 7z CLI (cached)
        4. Copy to work_dir for this test run
        """
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

        # Get portable version
        portable_config = config.windows_portable
        version = portable_config.comfyui_portable_version or "latest"

        if version == "latest":
            version = self._get_latest_release_tag()

        # Use persistent cache directory
        cache_dir = _get_cache_dir()
        archive_path = cache_dir / f"ComfyUI_portable_{version}.7z"
        cached_extract_dir = cache_dir / f"ComfyUI_portable_{version}"

        # Download if not cached
        if not archive_path.exists():
            self._download_portable(version, archive_path)
        else:
            self._log(f"Using cached archive: {archive_path}")

        # Extract if not cached (check for ComfyUI_windows_portable subdir)
        if not cached_extract_dir.exists() or not any(cached_extract_dir.iterdir()):
            self._log(f"Extracting to cache: {cached_extract_dir}")
            if cached_extract_dir.exists():
                shutil.rmtree(cached_extract_dir)
            self._extract_7z(archive_path, cached_extract_dir)
        else:
            self._log(f"Using cached extraction: {cached_extract_dir}")

        # Copy from cache to work directory (much faster than re-extracting 7z)
        import uuid
        portable_work_dir = Path.home() / "Desktop" / "portabletest"
        if portable_work_dir.exists():
            # Rename old folder and delete in background (faster than blocking rmtree)
            old_name = f"portabletest_old_{uuid.uuid4().hex[:8]}"
            old_path = Path.home() / "Desktop" / old_name
            self._log(f"Moving old folder to {old_name} (deleting in background)...")
            portable_work_dir.rename(old_path)
            # Delete in background using cmd /c rd (faster than shutil.rmtree)
            subprocess.Popen(
                ["cmd", "/c", "rd", "/s", "/q", str(old_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._log(f"Copying from cache to: {portable_work_dir}")
        shutil.copytree(cached_extract_dir, portable_work_dir)
        extract_dir = portable_work_dir

        # Find ComfyUI directory inside extracted archive
        # Structure is usually: ComfyUI_windows_portable/ComfyUI/
        comfyui_dir = self._find_comfyui_dir(extract_dir)
        if not comfyui_dir:
            raise SetupError(
                "Could not find ComfyUI directory in portable archive",
                f"Searched in: {extract_dir}"
            )

        # Create custom_nodes directory (may not exist in portable archive)
        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        # Find embedded Python
        python_embeded = extract_dir / "python_embeded"
        if not python_embeded.exists():
            # Try alternative location
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

        # On Linux, make Windows executables executable (for cross-platform testing)
        import sys
        if sys.platform != "win32":
            import stat
            for exe in python_embeded.glob("*.exe"):
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            for dll in python_embeded.glob("*.dll"):
                dll.chmod(dll.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        # Install uv into embedded Python for faster package installs
        self._log("Installing uv into embedded Python...")
        self._run_command(
            [str(python), "-m", "pip", "install", "uv"],
            cwd=comfyui_dir,
        )

        # Install ComfyUI's requirements (portable may be missing newer deps like sqlalchemy)
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

    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """
        Install custom node into ComfyUI Portable.

        1. Copy to custom_nodes/
        2. Build local dev wheels (comfy-env, etc.)
        3. Install requirements.txt if present (with local wheels)
        4. Run install.py if present (using embedded Python)
        """
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name

        target_dir = paths.custom_nodes_dir / node_name

        # Copy node directory (ignore work_dir and common non-source dirs to avoid recursion)
        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(node_dir, target_dir, ignore=_gitignore_filter(node_dir, paths.work_dir))

        # Build local dev wheels (comfy-env with junction fix, etc.)
        wheel_dir = _build_local_wheels(paths.work_dir, self._log)
        self._wheel_dir = wheel_dir  # Store for _uv_install

        # Install local wheels FIRST with --force-reinstall to override any cached PyPI versions
        # This ensures comfy-env with junction fix is used instead of PyPI version
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

        # Install requirements.txt (install.py may depend on these)
        # Uses pip (not uv) to match user experience
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._pip_install(paths.python, ["-r", str(requirements_file)], target_dir)

        # Run install.py if present
        install_py = target_dir / "install.py"
        if install_py.exists():
            self._log("Running install.py...")
            # Set CUDA version for CPU-only CI (comfy-env will use this if no GPU detected)
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
            "-s",  # Don't add user site-packages
            str(paths.comfyui_dir / "main.py"),
            "--listen", "127.0.0.1",
            "--port", str(port),
            "--windows-standalone-build",  # Required for portable
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
        """Clean up test environment."""
        self._log(f"Cleaning up {paths.work_dir}...")

        if paths.work_dir.exists():
            try:
                shutil.rmtree(paths.work_dir)
            except PermissionError:
                self._log("Warning: Could not fully clean up (files may be locked)")

    def _get_latest_release_tag(self) -> str:
        """Get the latest release tag from GitHub API."""
        self._log("Fetching latest release version...")

        # Use GITHUB_TOKEN if available (raises rate limit from 60 to 1000/hr)
        headers = {}
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        try:
            response = requests.get(PORTABLE_LATEST_API, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            tag = data.get("tag_name", "")
            if not tag:
                raise DownloadError("No tag_name in release response")
            self._log(f"Latest version: {tag}")
            return tag
        except requests.RequestException as e:
            raise DownloadError(
                "Failed to fetch latest release info",
                PORTABLE_LATEST_API
            ) from e

    def _download_portable(self, version: str, dest: Path) -> None:
        """Download ComfyUI portable archive."""
        url = PORTABLE_RELEASE_URL.format(version=version)
        self._log(f"Downloading portable ComfyUI from {url}...")

        # Use GITHUB_TOKEN if available for release asset downloads
        headers = {}
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        try:
            response = requests.get(url, headers=headers, stream=True, timeout=300)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0
            last_logged = 0

            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = int((downloaded / total_size) * 100)
                        if percent >= last_logged + 10:
                            self._log(f"  Downloaded: {percent}%")
                            last_logged = percent

            self._log(f"Downloaded to {dest}")

        except requests.RequestException as e:
            raise DownloadError(
                f"Failed to download portable ComfyUI {version}",
                url
            ) from e

    def _find_7z_executable(self) -> Optional[str]:
        """Find 7z executable on the system."""
        # Check if 7z is in PATH
        if shutil.which("7z"):
            return "7z"

        # Common Windows installation paths
        import sys
        if sys.platform == "win32":
            common_paths = [
                Path(r"C:\Program Files\7-Zip\7z.exe"),
                Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
                Path.home() / "AppData" / "Local" / "Programs" / "7-Zip" / "7z.exe",
            ]
            for path in common_paths:
                if path.exists():
                    return str(path)

        return None

    def _extract_7z(self, archive: Path, dest: Path) -> None:
        """Extract 7z archive using 7z CLI or py7zr."""
        self._log(f"Extracting {archive.name}...")

        # Try 7z command first (handles BCJ2 filter that py7zr doesn't support)
        seven_z = self._find_7z_executable()
        if seven_z:
            dest.mkdir(parents=True, exist_ok=True)
            self._log(f"Using 7z: {seven_z}")
            result = subprocess.run(
                [seven_z, "x", str(archive), f"-o{dest}", "-y"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self._log(f"Extracted to {dest}")
                return
            else:
                self._log(f"7z failed: {result.stderr}")
            # Fall through to py7zr if 7z fails

        # Fallback to py7zr (note: doesn't support BCJ2 filter used in ComfyUI portable)
        try:
            import py7zr
            with py7zr.SevenZipFile(archive, mode="r") as z:
                z.extractall(path=dest)
            self._log(f"Extracted to {dest}")
        except ImportError:
            raise SetupError(
                "7z command not found and py7zr not installed",
                "Install 7-Zip (https://7-zip.org) or run: pip install py7zr"
            )
        except Exception as e:
            raise SetupError(
                f"Failed to extract {archive}",
                f"{e}\n\nNote: ComfyUI portable uses BCJ2 compression which requires 7-Zip.\n"
                f"Install 7-Zip from https://7-zip.org"
            )

    def _find_comfyui_dir(self, extract_dir: Path) -> Optional[Path]:
        """Find ComfyUI directory within extracted archive."""
        # Check common locations
        candidates = [
            extract_dir / "ComfyUI",
            extract_dir / "ComfyUI_windows_portable" / "ComfyUI",
        ]

        # Also check first-level subdirectories
        for subdir in extract_dir.iterdir():
            if subdir.is_dir():
                candidates.append(subdir / "ComfyUI")

        for candidate in candidates:
            if candidate.exists() and (candidate / "main.py").exists():
                return candidate

        return None

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

        # Install requirements.txt first (using pip to match user experience)
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log(f"  Installing {name} requirements...")
            self._pip_install(paths.python, ["-r", str(requirements_file)], target_dir)

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
