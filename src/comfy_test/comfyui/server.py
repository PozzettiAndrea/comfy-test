"""ComfyUI server management."""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable, List, TYPE_CHECKING

from .api import ComfyUIAPI
from ..common.errors import ServerError, TestTimeoutError

if TYPE_CHECKING:
    from ..common.base_platform import TestPaths, TestPlatform
    from ..common.config import TestConfig


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
        port: Optional[int] = None,
        cuda_mock_packages: Optional[List[str]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        env_vars: Optional[dict] = None,
        novram: bool = False,
        vram_debug: bool = False,
    ):
        self.platform = platform
        self.paths = paths
        self.config = config
        # Use random port to avoid conflicts with user's regular ComfyUI (8188)
        if port is None:
            import random
            port = random.randint(41880, 41899)
        self.port = port
        self.cuda_mock_packages = cuda_mock_packages or []
        self.env_vars = env_vars or {}
        self.novram = novram
        self.vram_debug = vram_debug
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

    @property
    def pid(self) -> int | None:
        """Get server process PID, or None if not running."""
        return self._process.pid if self._process else None

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

    def start(self, wait_timeout: int = 600) -> None:
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

        # Prepare extra env vars
        extra_env = {}

        # Always enable comfy-env debug logging in tests
        extra_env["COMFY_ENV_DEBUG"] = "1"

        # Add env_vars from comfy-env.toml (CI only)
        if self.env_vars:
            extra_env.update(self.env_vars)

        # VRAM debug logging
        if self.vram_debug:
            extra_env["COMFY_VRAM_DEBUG"] = "1"

        # Add CUDA mock injection
        if self.cuda_mock_packages:
            extra_env["COMFY_TEST_MOCK_PACKAGES"] = ",".join(self.cuda_mock_packages)
            extra_env["COMFY_TEST_STRICT_IMPORTS"] = "1"
            self._log(f"CUDA mock packages: {', '.join(self.cuda_mock_packages)}")

        extra_args = []
        if self.novram:
            extra_args.append("--novram")

        self._process = self.platform.start_server(
            self.paths,
            self.config,
            self.port,
            extra_env=extra_env,
            extra_args=extra_args,
        )

        # Start output reader thread
        self._stop_output_thread = False
        self._output_thread = threading.Thread(target=self._read_output, daemon=True)
        self._output_thread.start()

        # Wait for server to be ready
        self._wait_for_ready(wait_timeout)

    def _read_output(self) -> None:
        """Read and log server output using threads (Windows-compatible)."""
        if not self._process:
            return

        # Some platforms (windows_portable) redirect the server's stdout/stderr
        # straight to a file to avoid the Windows pipe-buffer deadlock, leaving
        # self._process.stdout/stderr as None. In that case, tail the platform-
        # advertised log file so we still get live streaming, captured output
        # for diagnostics, and import-error detection.
        if self._process.stdout is None:
            log_path = getattr(self.platform, "server_log_path", None)
            if log_path is None:
                return
            self._tail_log_file(Path(log_path))
            return

        def read_stream(stream, name):
            """Read from a stream and log each line."""
            try:
                for line in iter(stream.readline, ''):
                    if self._stop_output_thread:
                        break
                    if line:
                        line_text = line.rstrip()
                        self._output_lines.append(line_text)
                        self._log_all(f"  [ComfyUI] {line_text}")
            except Exception as exc:
                self._log_all(f"  [ComfyUI:{name}] reader thread died: {exc!r}")

        # Start separate threads for stdout and stderr
        stdout_thread = threading.Thread(target=read_stream, args=(self._process.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=read_stream, args=(self._process.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Wait for process to end or stop signal
        while not self._stop_output_thread:
            if self._process.poll() is not None:
                # Give threads a moment to finish reading
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                break
            time.sleep(0.1)

    def _tail_log_file(self, log_path: Path) -> None:
        """Tail a server log file written by the platform (used when stdout/stderr are redirected)."""
        deadline = time.time() + 5
        while not log_path.exists() and time.time() < deadline:
            if self._stop_output_thread:
                return
            time.sleep(0.1)
        if not log_path.exists():
            self._log_all(f"  [ComfyUI] log file never appeared: {log_path}")
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                while not self._stop_output_thread:
                    line = fh.readline()
                    if line:
                        line_text = line.rstrip()
                        self._output_lines.append(line_text)
                        self._log_all(f"  [ComfyUI] {line_text}")
                        continue
                    if self._process.poll() is not None:
                        # Drain anything written between our last read and exit.
                        for remaining in fh:
                            line_text = remaining.rstrip()
                            self._output_lines.append(line_text)
                            self._log_all(f"  [ComfyUI] {line_text}")
                        return
                    time.sleep(0.1)
        except Exception as exc:
            self._log_all(f"  [ComfyUI] log tail died: {exc!r}")

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
                # Let output thread finish reading remaining output
                if self._output_thread:
                    self._stop_output_thread = True
                    self._output_thread.join(timeout=5)  # Give threads time to finish

                # Include captured output in error for debugging
                output_tail = "\n".join(self._output_lines[-50:]) if self._output_lines else "(no output captured)"
                raise ServerError(
                    "ComfyUI server exited unexpectedly",
                    f"Exit code: {self._process.returncode}\n\nServer output (last 50 lines):\n{output_tail}"
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
