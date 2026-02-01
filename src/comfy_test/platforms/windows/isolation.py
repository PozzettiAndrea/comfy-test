"""Environment isolation for Windows testing without Docker.

This module provides isolation utilities for running tests on Windows
machines that don't support Docker (e.g., Shadow Gaming VMs).

Key features:
- Environment variable save/restore
- Isolated TEMP directory per test
- DLL search path isolation (Python 3.8+)
- Orphaned process cleanup
"""

import os
import sys
import subprocess
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List, Generator


def cleanup_comfy_processes() -> int:
    """Kill any orphaned ComfyUI/Python processes from previous tests.

    Returns:
        Number of processes killed
    """
    if sys.platform != "win32":
        return 0

    # Get list of python processes with ComfyUI in command line
    try:
        # Use Get-CimInstance (modern) instead of Get-WmiObject (deprecated)
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
             "Where-Object {$_.Name -eq 'python.exe' -and $_.CommandLine -like '*ComfyUI*'} | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0 or not result.stdout.strip():
            return 0

        pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]

        # Kill each process
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/PID", pid],
                capture_output=True,
                timeout=10
            )

        return len(pids)

    except (subprocess.TimeoutExpired, Exception):
        # Fallback: silently ignore errors - process cleanup is best-effort
        return 0


def cleanup_temp_files(work_dir: Path) -> None:
    """Clean up temporary files from a test work directory.

    Args:
        work_dir: The test work directory to clean
    """
    temp_dir = work_dir / "temp"
    if temp_dir.exists():
        import shutil
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


@contextmanager
def isolated_environment(
    work_dir: Path,
    dll_dirs: Optional[List[Path]] = None,
    clean_env_prefixes: Optional[List[str]] = None,
    preserve_env_vars: Optional[List[str]] = None,
) -> Generator[None, None, None]:
    """Context manager that isolates environment for a test run.

    This provides Docker-like isolation on Windows without needing Docker:
    - Saves and restores all environment variables
    - Sets isolated TEMP/TMP directories
    - Isolates DLL search path (Python 3.8+)

    Args:
        work_dir: Working directory for the test (used for isolated temp)
        dll_dirs: Additional DLL directories to add to search path
        clean_env_prefixes: Environment variable prefixes to clear (default: CUDA_, TORCH_, COMFY)
        preserve_env_vars: Env vars to preserve even if they match clean prefixes

    Example:
        with isolated_environment(Path("/tmp/test")):
            # Run test with isolated environment
            run_comfy_test()
        # Environment is restored here
    """
    if sys.platform != "win32":
        # On non-Windows, just yield (no isolation needed, Docker works)
        yield
        return

    # Default prefixes to clean
    if clean_env_prefixes is None:
        clean_env_prefixes = ["CUDA_", "TORCH_", "COMFY"]

    # Default vars to preserve
    if preserve_env_vars is None:
        preserve_env_vars = [
            "COMFY_TEST_GPU",
            "COMFY_ENV_CUDA_VERSION",
            "CUDA_VISIBLE_DEVICES",
            "COMFY_TEST_IN_DOCKER",
        ]

    # Save original state
    saved_env = os.environ.copy()
    saved_path = os.environ.get("PATH", "")
    dll_handles = []

    try:
        # 1. Create and set isolated temp directory (use absolute path for subprocess compatibility)
        temp_dir = (work_dir / "temp").resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = str(temp_dir)
        os.environ["TMP"] = str(temp_dir)

        # 2. Clear potentially polluting env vars
        for key in list(os.environ.keys()):
            # Check if this key matches any prefix to clean
            should_clean = any(key.startswith(prefix) for prefix in clean_env_prefixes)
            should_preserve = key in preserve_env_vars

            if should_clean and not should_preserve:
                del os.environ[key]

        # 3. Prepend DLL directories to PATH
        if dll_dirs:
            dll_paths = [str(d) for d in dll_dirs if d.exists()]
            if dll_paths:
                os.environ["PATH"] = ";".join(dll_paths) + ";" + saved_path

        # 4. Add DLL directories using os.add_dll_directory (Python 3.8+)
        # This affects how Python extension modules find their DLLs
        if sys.version_info >= (3, 8) and dll_dirs:
            for dll_dir in dll_dirs:
                if dll_dir.exists():
                    try:
                        handle = os.add_dll_directory(str(dll_dir))
                        dll_handles.append(handle)
                    except OSError:
                        # Directory might not exist or be accessible
                        pass

        yield

    finally:
        # Restore environment
        os.environ.clear()
        os.environ.update(saved_env)

        # Close DLL directory handles
        for handle in dll_handles:
            try:
                handle.close()
            except Exception:
                pass


class WindowsIsolation:
    """Helper class for managing Windows test isolation.

    This class wraps the isolation utilities and provides a cleaner
    interface for the test manager.

    Example:
        isolation = WindowsIsolation(work_dir)
        isolation.setup()
        try:
            # Run tests
            pass
        finally:
            isolation.teardown()
    """

    def __init__(self, work_dir: Path, log_callback=None):
        self.work_dir = work_dir
        self._log = log_callback or (lambda msg: None)
        self._saved_env = None
        self._dll_handles = []
        self._active = False

    def setup(self) -> None:
        """Set up isolation (call before tests)."""
        if sys.platform != "win32":
            return

        self._log("Setting up Windows isolation...")

        # Clean up any orphaned processes from previous runs (local only)
        # Skip in CI - runners are fresh and the filter can accidentally match comfy-test itself
        if not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS"):
            killed = cleanup_comfy_processes()
            if killed:
                self._log(f"  Cleaned up {killed} orphaned process(es)")

        # Save environment
        self._saved_env = os.environ.copy()

        # Set up isolated temp (use absolute path to avoid issues with subprocess CWD)
        temp_dir = (self.work_dir / "temp").resolve()
        temp_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = str(temp_dir)
        os.environ["TMP"] = str(temp_dir)
        self._log(f"  Isolated TEMP: {temp_dir}")

        # Clear polluting env vars
        cleaned = []
        preserve = {"COMFY_TEST_GPU", "COMFY_ENV_CUDA_VERSION", "CUDA_VISIBLE_DEVICES", "COMFY_TEST_IN_DOCKER"}
        for key in list(os.environ.keys()):
            if key.startswith(("CUDA_", "TORCH_", "COMFY")) and key not in preserve:
                del os.environ[key]
                cleaned.append(key)
        if cleaned:
            self._log(f"  Cleared {len(cleaned)} env var(s): {', '.join(cleaned[:5])}{'...' if len(cleaned) > 5 else ''}")

        self._active = True
        self._log("  Isolation active")

    def teardown(self) -> None:
        """Tear down isolation (call after tests)."""
        if sys.platform != "win32" or not self._active:
            return

        self._log("Tearing down Windows isolation...")

        # Restore environment
        if self._saved_env:
            os.environ.clear()
            os.environ.update(self._saved_env)
            self._log("  Environment restored")

        # Close DLL handles
        for handle in self._dll_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._dll_handles.clear()

        # Clean up orphaned processes (local only)
        # Skip in CI - runners are fresh and the filter can accidentally match comfy-test itself
        if not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS"):
            killed = cleanup_comfy_processes()
            if killed:
                self._log(f"  Cleaned up {killed} orphaned process(es)")

        # Clean up temp files
        cleanup_temp_files(self.work_dir)

        self._active = False
        self._log("  Isolation torn down")

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, *args):
        self.teardown()
