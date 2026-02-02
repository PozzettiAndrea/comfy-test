"""CI-specific behavior for Windows platform."""

from pathlib import Path


def get_ci_python() -> Path:
    """Get the CI venv Python path for GitHub Actions.

    Returns:
        Path to Python executable in the CI venv.
    """
    return Path.home() / "venv" / "venv" / "Scripts" / "python.exe"
