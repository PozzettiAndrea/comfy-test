"""Common utilities shared across all platforms.

This module contains functions that are identical across Linux, Windows,
macOS, and Windows Portable platforms.
"""

import os
from pathlib import Path
from typing import Dict, Optional


# =============================================================================
# CI Environment Detection
# =============================================================================

def is_ci_environment() -> bool:
    """Check if running in a CI environment."""
    return os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


def get_ci_env_vars() -> Dict[str, str]:
    """Get environment variables specific to CI runs.

    Returns:
        Dict of environment variables to set in CI.
    """
    env = {}
    if os.environ.get("GITHUB_ACTIONS"):
        env["CI"] = "true"
    return env


# =============================================================================
# GPU Mode Detection
# =============================================================================

def is_gpu_mode_enabled() -> bool:
    """Check if GPU mode is enabled via environment variable."""
    return bool(os.environ.get("COMFY_TEST_GPU"))


# =============================================================================
# Local Development Utilities
# =============================================================================

def get_local_wheels_path() -> Optional[Path]:
    """Get path to local wheels directory if set.

    Returns:
        Path to local wheels or None if not set.
    """
    local_wheels = os.environ.get("COMFY_LOCAL_WHEELS")
    if local_wheels:
        path = Path(local_wheels)
        if path.exists():
            return path
    return None
