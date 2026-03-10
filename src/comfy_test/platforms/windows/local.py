"""Local-specific behavior for Windows platform."""

import os
from pathlib import Path


def get_local_dev_packages():
    """Get list of local dev packages that exist.

    Returns:
        List of (name, path) tuples for existing packages.
    """
    utils_dir = Path(os.environ["COMFY_TEST_LOCAL_UTILS"]) if os.environ.get("COMFY_TEST_LOCAL_UTILS") else None
    if not utils_dir:
        return []
    packages = [
        ("comfy-env", utils_dir / "comfy-env"),
        ("comfy-test", utils_dir / "comfy-test"),
    ]
    return [(name, path) for name, path in packages if path.exists()]
