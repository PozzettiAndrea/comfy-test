"""Local-specific behavior for Linux platform."""

import subprocess


def detect_gpu() -> bool:
    """Detect if NVIDIA GPU is available.

    Returns:
        True if nvidia-smi is available and returns success.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
