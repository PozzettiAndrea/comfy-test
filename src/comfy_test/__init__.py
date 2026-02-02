from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("comfy-test")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

from .common.config import TestConfig, WorkflowConfig, PlatformTestConfig
from .common.config_file import load_config, discover_config, CONFIG_FILE_NAMES
from .orchestration.manager import TestManager
from .orchestration.results import TestResult
from .common.errors import (
    TestError,
    ConfigError,
    SetupError,
    ServerError,
    WorkflowError,
    VerificationError,
    TestTimeoutError,
    DownloadError,
)

# Convenience functions
from .runner import run_tests

__all__ = [
    # Config
    "TestConfig",
    "WorkflowConfig",
    "PlatformTestConfig",
    "load_config",
    "discover_config",
    "CONFIG_FILE_NAMES",
    # Manager
    "TestManager",
    "TestResult",
    # Errors
    "TestError",
    "ConfigError",
    "SetupError",
    "ServerError",
    "WorkflowError",
    "VerificationError",
    "TestTimeoutError",
    "DownloadError",
    # Convenience
    "run_tests",
]
