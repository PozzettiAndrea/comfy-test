"""CI-specific behavior for Linux platform."""

from pathlib import Path
from typing import Dict


def get_ci_cache_paths(work_dir: Path) -> Dict[str, Path]:
    """Get paths that should be cached in CI.

    Args:
        work_dir: Working directory for test artifacts

    Returns:
        Dict mapping cache key to path.
    """
    return {
        "pip": Path.home() / ".cache" / "pip",
        "uv": Path.home() / ".cache" / "uv",
    }
