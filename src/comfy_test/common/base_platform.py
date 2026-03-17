"""Abstract base class for platform-specific test operations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING
import subprocess

if TYPE_CHECKING:
    from .config import TestConfig


@dataclass
class TestPaths:
    """Platform-specific paths for test environment.

    Attributes:
        work_dir: Working directory for test artifacts
        comfyui_dir: ComfyUI installation directory
        python: Python executable path
        custom_nodes_dir: custom_nodes/ directory
    """

    work_dir: Path
    comfyui_dir: Path
    python: Path
    custom_nodes_dir: Path


class TestPlatform(ABC):
    """
    Abstract base class for platform-specific test operations.

    Each platform (Linux, Windows, WindowsPortable, macOS) implements this
    to provide consistent test behavior across operating systems.
    """

    def __init__(self, log_callback: Optional[Callable[[str], None]] = None):
        """
        Initialize platform provider.

        Args:
            log_callback: Optional callback for logging messages
        """
        self._log = log_callback or (lambda msg: print(msg))

    @property
    @abstractmethod
    def name(self) -> str:
        """Platform name: 'linux', 'windows', 'windows_portable', 'macos'."""
        pass

    @property
    @abstractmethod
    def executable_suffix(self) -> str:
        """Executable suffix: '' for Unix, '.exe' for Windows."""
        pass

    @abstractmethod
    def setup_comfyui(self, config: "TestConfig", work_dir: Path) -> TestPaths:
        """
        Set up ComfyUI for testing.

        For Linux/Windows: clone repo, install deps to system Python
        For Portable: download and extract 7z

        Args:
            config: Test configuration
            work_dir: Working directory for test artifacts

        Returns:
            TestPaths with all necessary paths
        """
        pass

    @abstractmethod
    def install_node(self, paths: TestPaths, node_dir: Path, deps_installed: bool = False) -> None:
        """
        Install the custom node into ComfyUI.

        - Copy/symlink to custom_nodes/
        - Run install.py if present (unless deps_installed)
        - Install requirements.txt (unless deps_installed)

        Args:
            paths: TestPaths from setup_comfyui
            node_dir: Path to custom node source directory
            deps_installed: If True, skip requirements.txt and install.py
        """
        pass

    @abstractmethod
    def start_server(
        self,
        paths: TestPaths,
        config: "TestConfig",
        port: int = 8188,
        extra_env: Optional[dict] = None,
        extra_args: Optional[list[str]] = None,
    ) -> subprocess.Popen:
        """
        Start ComfyUI server.

        Args:
            paths: TestPaths from setup_comfyui
            config: Test configuration
            port: Port to listen on
            extra_env: Additional environment variables

        Returns:
            subprocess.Popen handle for the running server
        """
        pass

    def stop_server(self, process: subprocess.Popen) -> None:
        """
        Stop ComfyUI server.

        Args:
            process: Process handle from start_server
        """
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    @abstractmethod
    def cleanup(self, paths: TestPaths) -> None:
        """
        Clean up test environment.

        Args:
            paths: TestPaths from setup_comfyui
        """
        pass

    @abstractmethod
    def install_node_from_repo(self, paths: TestPaths, repo: str, name: str) -> None:
        """
        Install a custom node from a GitHub repository.

        1. Git clone into custom_nodes/
        2. Install requirements.txt if present
        3. Run install.py if present

        Args:
            paths: TestPaths from setup_comfyui
            repo: GitHub repo path, e.g., 'PozzettiAndrea/ComfyUI-GeometryPack'
            name: Name for the node directory
        """
        pass

    def _run_command(
        self,
        cmd: list[str],
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """
        Run a command with logging.

        Args:
            cmd: Command and arguments
            cwd: Working directory
            env: Environment variables
            check: Raise on non-zero exit

        Returns:
            CompletedProcess result
        """
        self._log(f"Running: {' '.join(str(c) for c in cmd)}")

        import os
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        # Stream stdout line-by-line so users see progress live
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        import threading

        def _read_stderr():
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            line_text = line.rstrip("\n")
            stdout_lines.append(line_text)
            self._log(f"  {line_text}")

        proc.wait()
        stderr_thread.join(timeout=5)

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "".join(stderr_lines)

        if proc.returncode != 0 and check:
            self._log(f"Command failed with code {proc.returncode}")
            if stderr_text:
                self._log(f"stderr: {stderr_text}")
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, stdout_text, stderr_text
            )

        return subprocess.CompletedProcess(
            cmd, proc.returncode, stdout_text, stderr_text
        )
