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


# ComfyUI portable release URL pattern
PORTABLE_RELEASE_URL = "https://github.com/comfyanonymous/ComfyUI/releases/download/{version}/ComfyUI_windows_portable_nvidia.7z"
PORTABLE_LATEST_API = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"


class WindowsPortableTestPlatform(TestPlatform):
    """Windows Portable platform implementation for ComfyUI testing."""

    @property
    def name(self) -> str:
        return "windows_portable"

    @property
    def executable_suffix(self) -> str:
        return ".exe"

    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI Portable for testing on Windows.

        1. Determine version to download
        2. Download 7z archive from GitHub releases
        3. Extract with py7zr
        """
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # Get portable version
        portable_config = config.windows_portable
        version = portable_config.comfyui_portable_version or "latest"

        if version == "latest":
            version = self._get_latest_release_tag()

        # Download portable archive
        archive_path = work_dir / f"ComfyUI_portable_{version}.7z"
        if not archive_path.exists():
            self._download_portable(version, archive_path)

        # Extract archive
        extract_dir = work_dir / "ComfyUI_portable"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        self._extract_7z(archive_path, extract_dir)

        # Find ComfyUI directory inside extracted archive
        # Structure is usually: ComfyUI_windows_portable/ComfyUI/
        comfyui_dir = self._find_comfyui_dir(extract_dir)
        if not comfyui_dir:
            raise SetupError(
                "Could not find ComfyUI directory in portable archive",
                f"Searched in: {extract_dir}"
            )

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
        custom_nodes_dir = comfyui_dir / "custom_nodes"
        custom_nodes_dir.mkdir(exist_ok=True)

        return TestPaths(
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            python=python,
            custom_nodes_dir=custom_nodes_dir,
            venv_dir=None,  # Portable doesn't use venv
        )

    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """
        Install custom node into ComfyUI Portable.

        1. Copy to custom_nodes/
        2. Run install.py if present (using embedded Python)
        3. Install requirements.txt if present
        """
        node_dir = Path(node_dir).resolve()
        node_name = node_dir.name

        target_dir = paths.custom_nodes_dir / node_name

        # Copy node directory
        self._log(f"Copying {node_name} to custom_nodes/...")
        if target_dir.exists():
            shutil.rmtree(target_dir)

        shutil.copytree(node_dir, target_dir)

        # Install requirements.txt first (install.py may depend on these)
        requirements_file = target_dir / "requirements.txt"
        if requirements_file.exists():
            self._log("Installing node requirements...")
            self._run_command(
                [str(paths.python), "-m", "pip", "install",
                 "-r", str(requirements_file)],
                cwd=target_dir,
            )

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

        if config.cpu_only:
            cmd.append("--cpu")

        process = subprocess.Popen(
            cmd,
            cwd=paths.comfyui_dir,
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

    def _extract_7z(self, archive: Path, dest: Path) -> None:
        """Extract 7z archive using 7z CLI or py7zr."""
        self._log(f"Extracting {archive.name}...")

        # Try 7z command first (handles BCJ2 filter that py7zr doesn't support)
        if shutil.which("7z"):
            dest.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["7z", "x", str(archive), f"-o{dest}", "-y"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self._log(f"Extracted to {dest}")
                return
            # Fall through to py7zr if 7z fails

        # Fallback to py7zr
        try:
            import py7zr
            with py7zr.SevenZipFile(archive, mode="r") as z:
                z.extractall(path=dest)
            self._log(f"Extracted to {dest}")
        except ImportError:
            raise SetupError(
                "7z command not found and py7zr not installed",
                "Install 7-Zip or run: pip install py7zr"
            )
        except Exception as e:
            raise SetupError(
                f"Failed to extract {archive}",
                str(e)
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
