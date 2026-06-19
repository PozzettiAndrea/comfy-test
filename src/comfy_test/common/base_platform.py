"""Abstract base class for platform-specific test operations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, List, TYPE_CHECKING
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
        # Extra PyPI indexes (from config.extra_pip_indices), appended to install
        # commands as --extra-index-url. Populated by set_extra_pip_indices().
        self._extra_pip_indices: List[str] = []

    def set_extra_pip_indices(self, config: "TestConfig") -> None:
        """Capture extra PyPI indexes from config so install commands can add them
        as --extra-index-url (alongside the built-in PyTorch + PyPI indexes).
        Call this from setup_comfyui() before installing anything."""
        indices = getattr(config, "extra_pip_indices", None) or []
        if isinstance(indices, str):
            indices = [indices]
        self._extra_pip_indices = [str(i).strip() for i in indices if str(i).strip()]
        if self._extra_pip_indices:
            self._log(f"Extra pip indexes: {', '.join(self._extra_pip_indices)}")

    def _extra_index_args(self) -> List[str]:
        """uv/pip args adding each configured extra index as --extra-index-url."""
        args: List[str] = []
        for idx in self._extra_pip_indices:
            args.extend(["--extra-index-url", idx])
        return args

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
    def install_node(self, paths: TestPaths, node_dir: Path) -> None:
        """
        Install the custom node into ComfyUI.

        - Copy/symlink to custom_nodes/
        - Run install.py if present
        - Install requirements.txt

        Args:
            paths: TestPaths from setup_comfyui
            node_dir: Path to custom node source directory
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
        verbose: Optional[bool] = None,
        redact: Optional[list[str]] = None,
    ) -> subprocess.CompletedProcess:
        """
        Run a command with logging.

        By default, the subprocess's stdout is captured silently; only the
        `Running: ...` header and any error output (on non-zero exit) are
        logged. Set `COMFY_TEST_VERBOSE=1` (or `COMFY_ENV_DEBUG=1`) in the
        environment, or pass `verbose=True` per-call, to stream every stdout
        line live -- useful for `install.py` runs (which print structured
        progress) and for debugging slow pip installs.

        Args:
            cmd: Command and arguments
            cwd: Working directory
            env: Environment variables
            check: Raise on non-zero exit
            verbose: If True, stream stdout live regardless of env vars.
                If False, suppress streaming even if env vars are set.
                If None (default), follow env vars.
            redact: Substrings to mask as `***` in the logged "Running: ..."
                line AND in captured stdout/stderr printed on failure. Use
                this for secrets that legitimately appear in the actual cmd
                argv (e.g. PAT-embedded https://x-access-token:<pat>@...
                clone URLs) so they don't leak into session.log or CI logs.

        Returns:
            CompletedProcess result
        """
        import os

        def _mask(text: str) -> str:
            if not redact:
                return text
            for secret in redact:
                if secret:
                    text = text.replace(secret, "***")
            return text

        self._log(f"Running: {_mask(' '.join(str(c) for c in cmd))}")

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        if verbose is None:
            verbose = (
                os.environ.get("COMFY_TEST_VERBOSE", "").lower() in ("1", "true", "yes")
                or os.environ.get("COMFY_ENV_DEBUG", "").lower() in ("1", "true", "yes")
            )

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
            if verbose:
                self._log(f"  {_mask(line_text)}")

        proc.wait()
        stderr_thread.join(timeout=5)

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "".join(stderr_lines)

        if proc.returncode != 0 and check:
            # On failure, dump everything we captured so the actual error is
            # visible even when we suppressed live streaming.
            self._log(f"Command failed with code {proc.returncode}")
            if not verbose and stdout_text:
                self._log(f"stdout: {_mask(stdout_text)}")
            if stderr_text:
                self._log(f"stderr: {_mask(stderr_text)}")
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, stdout_text, stderr_text
            )

        return subprocess.CompletedProcess(
            cmd, proc.returncode, stdout_text, stderr_text
        )
