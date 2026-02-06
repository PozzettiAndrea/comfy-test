"""Local-specific behavior for Windows platform."""

from pathlib import Path


# Local dev packages to build wheels for
LOCAL_DEV_PACKAGES = [
    ("comfy-env", Path.home() / "Desktop" / "utils" / "comfy-env"),
    ("comfy-test", Path.home() / "Desktop" / "utils" / "comfy-test"),
]


def get_local_dev_packages():
    """Get list of local dev packages that exist.

    Returns:
        List of (name, path) tuples for existing packages.
    """
    return [(name, path) for name, path in LOCAL_DEV_PACKAGES if path.exists()]
