"""Local-specific behavior for Windows Portable platform."""

from pathlib import Path


def get_portable_cache_dir() -> Path:
    """Get the cache directory for portable downloads."""
    cache_dir = Path.home() / ".comfy-test" / "cache" / "portable"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
