"""Linux platform implementation for ComfyUI testing."""

from ..venv_server import VenvServerPlatform


class LinuxPlatform(VenvServerPlatform):
    """Linux: the venv-server defaults (bin/python, no exe suffix, --cpu lane)."""

    _name = "linux"
