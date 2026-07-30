"""Windows platform implementation for ComfyUI testing."""

import shutil
from pathlib import Path

from ..venv_server import VenvServerPlatform


class WindowsPlatform(VenvServerPlatform):
    """Windows: venv python under Scripts/python.exe; log pinned requirements;
    tolerate locked files on cleanup."""

    _name = "windows"
    _exe_suffix = ".exe"
    _venv_bindir = "Scripts"
    _venv_python_name = "python.exe"

    def _log_requirements_file(self, requirements_file: Path) -> None:
        """Print a requirements.txt indented so pinned versions are visible."""
        try:
            text = requirements_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        for line in text.splitlines():
            self._log(f"    {line}")

    def cleanup(self, paths) -> None:
        """Clean up, tolerating Windows file locks (vs the base's ignore_errors)."""
        self._log(f"Cleaning up {paths.work_dir}...")
        if paths.work_dir.exists():
            try:
                shutil.rmtree(paths.work_dir)
            except PermissionError:
                self._log("Warning: Could not fully clean up (files may be locked)")
