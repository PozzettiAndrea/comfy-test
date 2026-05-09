"""Settings for comfy-test.

Settings can be configured via:
  1. Environment variables (highest priority)
  2. Persistent settings in ~/.comfy-test/settings.env
  3. Defaults
"""

import os
import sys
from pathlib import Path

SETTINGS_FILE = Path.home() / ".comfy-test" / "settings.env"

# Load persistent settings (simple KEY=VALUE file) -- env vars always override
if SETTINGS_FILE.exists():
    try:
        for line in SETTINGS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

# General settings: (env_var, label)
GENERAL_SETTINGS = [
    ("COMFY_TEST_RUN_CONSUMER", "Run consumer tests (workflows/)"),
    ("COMFY_TEST_RUN_DEV", "Run dev tests (workflows/tests/)"),
    ("COMFY_TEST_SHOW_CONSOLE_ERRORS", "Show browser console errors in output"),
    ("COMFY_TEST_SHOW_CONSOLE_WARNINGS", "Show browser console warnings in output"),
    ("COMFY_TEST_VERBOSE", "Verbose output (show all ComfyUI server lines)"),
    ("COMFY_TEST_VRAM_DEBUG", "Enable VRAM debug logging"),
]

GENERAL_DEFAULTS = {
    "COMFY_TEST_RUN_CONSUMER": True,
    "COMFY_TEST_RUN_DEV": True,
    "COMFY_TEST_SHOW_CONSOLE_ERRORS": False,
    "COMFY_TEST_SHOW_CONSOLE_WARNINGS": False,
    "COMFY_TEST_VERBOSE": False,
    "COMFY_TEST_VRAM_DEBUG": False,
}

# Debug settings: (env_var, label)
DEBUG_SETTINGS = [
    ("COMFY_TEST_DBG_WORKER", "Debug worker subprocess IPC"),
    ("COMFY_TEST_DBG_SCREENSHOT", "Debug screenshot capture"),
    ("COMFY_TEST_DBG_WEBSOCKET", "Debug WebSocket messages"),
    ("COMFY_TEST_DBG_VALIDATION", "Debug workflow validation"),
]

DEBUG_DEFAULTS = {
    "COMFY_TEST_DBG_WORKER": False,
    "COMFY_TEST_DBG_SCREENSHOT": False,
    "COMFY_TEST_DBG_WEBSOCKET": False,
    "COMFY_TEST_DBG_VALIDATION": False,
}


_IS_WIN = sys.platform == "win32"

# Path settings: (env_var, label, default_value)
PATH_SETTINGS = [
    ("COMFY_TEST_LOCAL_UTILS", "Local utils directory (dev packages)", ""),
    ("COMFY_TEST_DOCKER_STAGE_DIR", "Docker build staging directory",
     r"D:\docker-stage" if _IS_WIN else "/tmp/comfy-test-docker-stage"),
    ("COMFY_TEST_INSTALLER_CACHE",
     "Cache directory for auto-downloaded driver/git installers (Windows only)",
     str(Path.home() / ".comfy-test" / "installers") if _IS_WIN else ""),
    ("COMFY_TEST_INSTALLERS_DIR",
     "Optional override directory for installers; leave empty for auto-download",
     ""),
    ("COMFY_TEST_DOCKER_ARTIFACT_PATH",
     "Where `docker build --save` writes the .tar.zst",
     ""),
]


def get_path(var: str, default: str = "") -> str:
    val = os.environ.get(var, "")
    return val if val else default


def _is_on(var: str, default: bool = False) -> bool:
    val = os.environ.get(var, "")
    if val == "":
        return default
    return val.lower() in ("1", "true", "yes")
