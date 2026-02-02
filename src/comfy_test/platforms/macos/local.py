"""Local-specific behavior for macOS platform."""

import platform


def detect_apple_silicon() -> bool:
    """Detect if running on Apple Silicon (M1/M2/M3).

    Returns:
        True if running on Apple Silicon, False otherwise.
    """
    return platform.machine() == "arm64"


def detect_mps_available() -> bool:
    """Detect if MPS (Metal Performance Shaders) is available.

    Returns:
        True if MPS is available for GPU acceleration.
    """
    try:
        import torch
        return torch.backends.mps.is_available()
    except (ImportError, AttributeError):
        return False
