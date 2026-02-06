"""Platform implementations for comfy-test.

This module contains OS-specific platform implementations:
- linux/: Linux platform
- windows/: Windows native platform
- windows_portable/: Windows Portable (embedded Python)
- macos/: macOS platform

Each platform provides CI and local execution modes.
"""

from .linux.platform import LinuxPlatform
from .windows.platform import WindowsPlatform
from .windows_portable.platform import WindowsPortablePlatform
from .macos.platform import MacOSPlatform

__all__ = [
    "LinuxPlatform",
    "WindowsPlatform",
    "WindowsPortablePlatform",
    "MacOSPlatform",
]
