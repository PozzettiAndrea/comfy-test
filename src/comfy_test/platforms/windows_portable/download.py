"""Download utilities for Windows Portable ComfyUI."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Callable

import requests

from ...common.errors import DownloadError, SetupError


# ComfyUI portable release URLs
PORTABLE_RELEASE_URL = "https://github.com/comfyanonymous/ComfyUI/releases/download/{version}/ComfyUI_windows_portable_nvidia.7z"
PORTABLE_LATEST_URL = "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z"
PORTABLE_LATEST_API = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"


def get_cache_dir() -> Path:
    """Get persistent cache directory for portable downloads."""
    cache_dir = Path.home() / ".comfy-test" / "cache" / "portable"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_latest_release_tag(log: Callable[[str], None]) -> str:
    """Get the latest release tag from GitHub API."""
    log("Fetching latest release version...")

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
        log(f"Latest version: {tag}")
        return tag
    except requests.RequestException as e:
        raise DownloadError(
            "Failed to fetch latest release info",
            PORTABLE_LATEST_API
        ) from e


def download_portable(version: str, dest: Path, log: Callable[[str], None]) -> None:
    """Download ComfyUI portable archive."""
    url = PORTABLE_RELEASE_URL.format(version=version)
    log(f"Downloading portable ComfyUI from {url}...")

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
                        log(f"  Downloaded: {percent}%")
                        last_logged = percent

        log(f"Downloaded to {dest}")

    except requests.RequestException as e:
        raise DownloadError(
            f"Failed to download portable ComfyUI {version}",
            url
        ) from e


def find_7z_executable() -> Optional[str]:
    """Find 7z executable on the system."""
    if shutil.which("7z"):
        return "7z"

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


def extract_7z(archive: Path, dest: Path, log: Callable[[str], None]) -> None:
    """Extract 7z archive using 7z CLI or py7zr."""
    log(f"Extracting {archive.name}...")

    seven_z = find_7z_executable()
    if seven_z:
        dest.mkdir(parents=True, exist_ok=True)
        log(f"Using 7z: {seven_z}")
        result = subprocess.run(
            [seven_z, "x", str(archive), f"-o{dest}", "-y"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log(f"Extracted to {dest}")
            return
        else:
            log(f"7z failed: {result.stderr}")

    # Fallback to py7zr
    try:
        import py7zr
        with py7zr.SevenZipFile(archive, mode="r") as z:
            z.extractall(path=dest)
        log(f"Extracted to {dest}")
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
