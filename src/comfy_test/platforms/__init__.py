"""Platform implementations for comfy-test.

This module contains OS-specific platform implementations:
- linux/: Linux platform
- windows/: Windows native platform
- windows_portable/: Windows Portable (embedded Python)
- macos/: macOS platform

Each platform provides CI and local execution modes.
"""

from .registry import (
    Platform,
    PLATFORMS,
    BY_ID,
    resolve,
    allowed_tokens,
    gallery_platforms,
    matrix,
)
from .linux.platform import LinuxPlatform
from .windows.platform import WindowsPlatform
from .windows_portable.platform import WindowsPortablePlatform
from .macos.platform import MacOSPlatform

__all__ = [
    # Platform registry (single source of truth for the platform taxonomy)
    "Platform",
    "PLATFORMS",
    "BY_ID",
    "resolve",
    "allowed_tokens",
    "gallery_platforms",
    "matrix",
    # OS-specific implementations
    "LinuxPlatform",
    "WindowsPlatform",
    "WindowsPortablePlatform",
    "MacOSPlatform",
]
