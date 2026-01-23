"""ComfyUI server management."""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable, List, TYPE_CHECKING

from .api import ComfyUIAPI
from ..errors import ServerError, TestTimeoutError

if TYPE_CHECKING:
    from ..test.platform.base import TestPaths, TestPlatform
    from ..test.config import TestConfig


class ComfyUIServer:
    """Manages ComfyUI server lifecycle.

    Handles starting, waiting for readiness, and stopping the ComfyUI server.

    Args:
        platform: Platform provider for server operations
        paths: Test paths from platform setup
        config: Test configuration
        port: Port to listen on
        cuda_mock_packages: List of CUDA packages to mock for import testing
        log_callback: Optional callback for logging

    Example:
        >>> with ComfyUIServer(platform, paths, config) as server:
        ...     api = server.get_api()
        ...     nodes = api.get_object_info()
    """

    def __init__(
        self,
        platform: "TestPlatform",
        paths: "TestPaths",
        config: "TestConfig",
        port: int = 8188,
        cuda_mock_packages: Optional[List[str]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        self.port = port
        self.cuda_mock_packages = cuda_mock_packages or []
        self._log = log_callback or (lambda msg: print(msg))
        self._extra_log_listeners: List[Callable[[str], None]] = []
        self._process: Optional[subprocess.Popen] = None
        self._api: Optional[ComfyUIAPI] = None
        self._output_thread: Optional[threading.Thread] = None
        self._stop_output_thread = False
        self._output_lines: List[str] = []  # Captured server output

    @property
    def base_url(self) -> str:
        """Get server base URL."""
        return f"http://127.0.0.1:{self.port}"

    def add_log_listener(self, callback: Callable[[str], None]) -> None:
        """Add an extra log listener for server output."""
        self._extra_log_listeners.append(callback)

    def remove_log_listener(self, callback: Callable[[str], None]) -> None:
        """Remove an extra log listener."""
        if callback in self._extra_log_listeners:
            self._extra_log_listeners.remove(callback)

    def _log_all(self, msg: str) -> None:
        """Log to main callback and all extra listeners."""
        self._log(msg)
        for listener in self._extra_log_listeners:
            listener(msg)

    def start(self, wait_timeout: int = 60) -> None:
        """Start the ComfyUI server and wait for it to be ready.

        Args:
            wait_timeout: Maximum seconds to wait for server to be ready

        Raises:
            ServerError: If server fails to start
            TestTimeoutError: If server doesn't become ready in time
        """
        if self._process is not None:
            raise ServerError("Server already started")

        self._log(f"Starting ComfyUI server on port {self.port}...")

        # Prepare extra env vars for CUDA mock injection
        extra_env = {}
        if self.cuda_mock_packages:
            extra_env["COMFY_TEST_MOCK_PACKAGES"] = ",".join(self.cuda_mock_packages)
            extra_env["COMFY_TEST_STRICT_IMPORTS"] = "1"
            self._log(f"CUDA mock packages: {', '.join(self.cuda_mock_packages)}")

        self._process = self.platform.start_server(
            self.paths,
            self.config,
            self.port,
            extra_env=extra_env,
        )

        # Start output reader thread
        self._stop_output_thread = False
        self._output_thread = threading.Thread(target=self._read_output, daemon=True)
        self._output_thread.start()

        # Wait for server to be ready
        self._wait_for_ready(wait_timeout)

    def _read_output(self) -> None:
        """Read and log server output in a background thread."""
        if not self._process:
            return
        try:
            # Read both stdout and stderr
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(self._process.stdout, selectors.EVENT_READ)
            sel.register(self._process.stderr, selectors.EVENT_READ)

            while not self._stop_output_thread:
                for key, _ in sel.select(timeout=0.1):
                    line = key.fileobj.readline()
                    if line:
                        line_text = line.rstrip()
                        self._output_lines.append(line_text)
                        self._log_all(f"  [ComfyUI] {line_text}")
                # Check if process ended
                if self._process.poll() is not None:
                    # Read remaining output
                    for line in self._process.stdout:
                        line_text = line.rstrip()
                        self._output_lines.append(line_text)
                        self._log_all(f"  [ComfyUI] {line_text}")
                    for line in self._process.stderr:
                        line_text = line.rstrip()
                        self._output_lines.append(line_text)
                        self._log_all(f"  [ComfyUI] {line_text}")
                    break
        except Exception:
            pass  # Process may have ended

    def _wait_for_ready(self, timeout: int) -> None:
        """Wait for server to become responsive.

        Args:
            timeout: Maximum seconds to wait

        Raises:
            TestTimeoutError: If server doesn't respond in time
            ServerError: If server process dies
        """
        self._log(f"Waiting for server to be ready (timeout: {timeout}s)...")
        api = ComfyUIAPI(self.base_url, timeout=5)

        start_time = time.time()
        last_error = None

        while time.time() - start_time < timeout:
            # Check if process died
            if self._process and self._process.poll() is not None:
                # Let output thread finish
                if self._output_thread:
                    self._stop_output_thread = True
                    self._output_thread.join(timeout=2)
                raise ServerError(
                    "ComfyUI server exited unexpectedly",
                    f"Exit code: {self._process.returncode}"
                )

            try:
                if api.health_check():
                    # Wait for nodes to fully load (health check passes before nodes load)
                    self._log("Server responding, waiting for nodes to load...")
                    time.sleep(20)
                    self._log("Server is ready!")
                    self._api = api
                    return
            except Exception as e:
                last_error = e

            time.sleep(1)

        # Timeout reached
        api.close()
        raise TestTimeoutError(
            f"Server did not become ready within {timeout} seconds",
            timeout_seconds=timeout,
        )

    def stop(self) -> None:
        """Stop the ComfyUI server."""
        if self._process is None:
            return

        self._log("Stopping ComfyUI server...")

        # Stop output thread
        if self._output_thread:
            self._stop_output_thread = True
            self._output_thread.join(timeout=2)
            self._output_thread = None

        if self._api:
            self._api.close()
            self._api = None

        self.platform.stop_server(self._process)
        self._process = None

    def get_api(self) -> ComfyUIAPI:
        """Get API client for the running server.

        Returns:
            ComfyUIAPI instance

        Raises:
            ServerError: If server is not running
        """
        if self._api is None:
            raise ServerError("Server is not running")
        return self._api

    def get_import_errors(self) -> List[str]:
        """Get list of import errors from server startup logs.

        Parses server output for "Cannot import" error messages that indicate
        custom node import failures.

        Returns:
            List of error messages (empty if no errors)
        """
        errors = []
        for line in self._output_lines:
            # ComfyUI logs import errors like:
            # "Cannot import <module_path> module for custom nodes: <error>"
            if "Cannot import" in line and "module for custom nodes" in line:
                errors.append(line)
            # Also catch general import errors in traceback
            elif "IMPORT FAILED" in line:
                errors.append(line)
        return errors

    def __enter__(self) -> "ComfyUIServer":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
