"""Test manager for orchestrating installation tests."""

import faulthandler
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, List

from ..common.config import TestConfig, TestLevel
from ..common.base_platform import TestPaths
from ..common.resource_monitor import ResourceMonitor
from ..common.comfy_env import get_cuda_packages, get_env_vars, get_node_reqs
from ..common.errors import (
    TestError, WorkflowExecutionError, WorkflowError, TestTimeoutError
)
from .results import (
    TestResult, has_gpu, get_hardware_info, get_workflow_timeout
)


class ProgressSpinner:
    """Progress indicator for workflow execution.

    Prints workflow start and completion status.
    """

    def __init__(self, workflow_name: str, current: int, total: int):
        self.workflow_name = workflow_name
        self.current = current
        self.total = total
        self.start_time = time.time()
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the spinner animation in a background thread."""
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        """Print workflow start - no animation."""
        line = f"executing {self.workflow_name} [{self.current}/{self.total}]"
        print(line)
        # Just wait until stop() is called
        while not self._stop:
            time.sleep(0.1)

    def stop(self, status: str) -> None:
        """Stop and print final status."""
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.3)
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        print(f"[{mins:02d}:{secs:02d}] {self.workflow_name} [{self.current}/{self.total}] - {status}")


# Platform imports - use the new structure
def get_platform(platform_name: str, log_callback=None):
    """Get platform instance by name."""
    if platform_name == "linux":
        from ..platforms.linux.platform import LinuxPlatform
        return LinuxPlatform(log_callback)
    elif platform_name == "windows":
        from ..platforms.windows.platform import WindowsPlatform
        return WindowsPlatform(log_callback)
    elif platform_name == "windows_portable":
        from ..platforms.windows_portable.platform import WindowsPortablePlatform
        return WindowsPortablePlatform(log_callback)
    elif platform_name == "macos":
        from ..platforms.macos.platform import MacOSPlatform
        return MacOSPlatform(log_callback)
    else:
        raise TestError(f"Unknown platform: {platform_name}")


class TestManager:
    """Orchestrates installation tests across platforms.

    Args:
        config: Test configuration
        node_dir: Path to custom node directory (default: current directory)
        log_callback: Optional callback for logging

    Example:
        >>> manager = TestManager(config)
        >>> results = manager.run_all()
        >>> for result in results:
        ...     print(f"{result.platform}: {'PASS' if result.success else 'FAIL'}")
    """

    # All possible levels in order
    ALL_LEVELS = [
        TestLevel.SYNTAX, TestLevel.INSTALL, TestLevel.REGISTRATION,
        TestLevel.INSTANTIATION, TestLevel.STATIC_CAPTURE, TestLevel.VALIDATION,
        TestLevel.EXECUTION
    ]

    def __init__(
        self,
        config: TestConfig,
        node_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.config = config
        self.node_dir = Path(node_dir) if node_dir else Path.cwd()
        self.output_dir = Path(output_dir) if output_dir else None  # External output dir
        self._original_log = log_callback or (lambda msg: print(msg.encode('ascii', errors='replace').decode('ascii') if isinstance(msg, str) else msg))
        self._session_log: List[str] = []  # Capture ALL logs for session.log
        self._session_start_time: float = 0  # Track start time for timestamps
        self._session_log_file: Optional[Path] = None  # Live log file
        self._level_index = 0
        self._total_levels = 0

    def _get_output_base(self) -> Path:
        """Get the base output directory for logs, screenshots, results."""
        return self.output_dir if self.output_dir else (self.node_dir / "comfy-test-results")

    def _log(self, msg: str) -> None:
        """Log message with timestamp, write to file immediately."""
        # Calculate elapsed time for timestamp
        if self._session_start_time:
            elapsed = time.time() - self._session_start_time
            mins, secs = divmod(int(elapsed), 60)
            timestamp = f"[{mins:02d}:{secs:02d}]"
        else:
            timestamp = "[00:00]"

        timestamped_msg = f"{timestamp} {msg}"
        self._original_log(msg)  # Print without timestamp (cleaner terminal)
        self._session_log.append(timestamped_msg)

        # Write to file immediately (live updates) with fsync to ensure crash survival
        if self._session_log_file:
            try:
                with open(self._session_log_file, "a", encoding="utf-8") as f:
                    f.write(timestamped_msg + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass  # Don't fail on log write errors

    def _save_session_log(self) -> None:
        """Log completion message (logs are written live now)."""
        if self._session_log_file and self._session_log_file.exists():
            self._original_log(f"Session log: {self._session_log_file}")

    def _log_level_start(self, level: TestLevel, in_config: bool) -> None:
        """Log the start of a test level with clear formatting."""
        self._level_index += 1
        level_name = level.value.upper()
        status = "" if in_config else " (implicit)"
        self._log("")  # Blank line before level
        self._log(f"[{self._level_index}/{self._total_levels}] {level_name}{status}")
        self._log("-" * 40)

    def _log_level_skip(self, level: TestLevel) -> None:
        """Log a skipped level."""
        self._level_index += 1
        level_name = level.value.upper()
        self._log(f"\n[{self._level_index}/{self._total_levels}] {level_name}: SKIPPED")

    def _log_level_done(self, level: TestLevel, message: str = "OK") -> None:
        """Log successful completion of a level."""
        level_name = level.value.upper()
        self._log(f"[{level_name}] {message}")

    def run_all(
        self,
        dry_run: bool = False,
        level: Optional[TestLevel] = None,
        workflow_filter: Optional[str] = None,
        comfyui_dir: Optional[Path] = None,
        skip_setup: bool = False,
        server_url: Optional[str] = None,
    ) -> List[TestResult]:
        """Run tests on all enabled platforms.

        Args:
            dry_run: If True, only show what would be done
            level: Maximum test level to run (None = all levels + workflows)
            workflow_filter: If specified, only run this workflow
            comfyui_dir: Use existing ComfyUI directory
            skip_setup: If True, skip node installation (assumes node is already installed)
            server_url: If provided, connect to existing server instead of starting one

        Returns:
            List of TestResult for each platform
        """
        results = []

        platforms = [
            ("linux", self.config.linux),
            ("macos", self.config.macos),
            ("windows", self.config.windows),
            ("windows_portable", self.config.windows_portable),
        ]

        for platform_name, platform_config in platforms:
            if not platform_config.enabled:
                self._log(f"Skipping {platform_name} (disabled)")
                continue

            result = self.run_platform(
                platform_name, dry_run, level, workflow_filter,
                comfyui_dir=comfyui_dir, skip_setup=skip_setup,
                server_url=server_url
            )
            results.append(result)

        return results

    def run_platform(
        self,
        platform_name: str,
        dry_run: bool = False,
        level: Optional[TestLevel] = None,
        workflow_filter: Optional[str] = None,
        comfyui_dir: Optional[Path] = None,
        skip_setup: bool = False,
        server_url: Optional[str] = None,
    ) -> TestResult:
        """Run tests on a specific platform.

        Args:
            platform_name: Platform to test ('linux', 'macos', 'windows', 'windows_portable')
            dry_run: If True, only show what would be done
            level: Maximum test level to run (CLI override, None = use config levels)
            workflow_filter: If specified, only run this workflow (e.g., 'fix_normals.json')
            comfyui_dir: Use existing ComfyUI directory
            skip_setup: If True, skip node installation (assumes node is already installed)
            server_url: If provided, connect to existing server instead of starting one

        Returns:
            TestResult for the platform
        """
        # Import here to avoid circular imports
        from ..comfyui.server import ComfyUIServer, ExternalComfyUIServer
        from ..comfyui.workflow import WorkflowRunner
        from ..platforms.windows.isolation import WindowsIsolation
        from ..reporting.screenshot import ScreenshotError

        # Normalize platform name (windows-portable -> windows_portable)
        platform_name = platform_name.lower().replace("-", "_")

        # Determine which levels to run
        # If CLI --level specified, filter to only levels up to that point
        requested_levels = self.config.levels
        if level:
            order = [TestLevel.SYNTAX, TestLevel.INSTALL, TestLevel.REGISTRATION,
                     TestLevel.INSTANTIATION, TestLevel.STATIC_CAPTURE, TestLevel.VALIDATION,
                     TestLevel.EXECUTION]
            max_idx = order.index(level)
            requested_levels = [l for l in requested_levels if order.index(l) <= max_idx]

        # Resolve dependencies (e.g., execution needs install)
        config_levels = TestLevel.resolve_dependencies(requested_levels)

        # Calculate total levels for progress display
        self._level_index = 0
        self._total_levels = len([l for l in self.ALL_LEVELS if l in config_levels])

        self._log(f"\n{'='*60}")
        self._log(f"Testing: {platform_name}")
        self._log(f"Levels: {', '.join(l.value for l in config_levels)}")
        self._log(f"{'='*60}")

        if dry_run:
            return self._dry_run(platform_name, config_levels)

        # Reset session log for this run and start timer
        self._session_log = []
        self._session_start_time = time.time()

        # Create session log file at output base level (not in logs/ subfolder)
        output_base = self._get_output_base()
        output_base.mkdir(parents=True, exist_ok=True)
        self._session_log_file = output_base / "session.log"
        self._session_log_file.write_text("", encoding="utf-8")  # Clear existing

        # Enable crash dump logging - captures Python stack trace on SIGSEGV/SIGABRT
        crash_log_path = output_base / "crash_dump.log"
        self._crash_log_file = open(crash_log_path, "w")
        faulthandler.enable(file=self._crash_log_file)
        self._log(f"Crash dump logging enabled: {crash_log_path}")

        try:
            # === SYNTAX LEVEL ===
            if TestLevel.SYNTAX not in config_levels:
                self._log_level_skip(TestLevel.SYNTAX)
            else:
                self._log_level_start(TestLevel.SYNTAX, TestLevel.SYNTAX in requested_levels)
                self._check_syntax()
                self._log_level_done(TestLevel.SYNTAX, "PASSED")

            # Check if we need install level for later levels
            needs_install = any(l in config_levels for l in [
                TestLevel.INSTALL, TestLevel.REGISTRATION,
                TestLevel.INSTANTIATION, TestLevel.STATIC_CAPTURE, TestLevel.VALIDATION,
                TestLevel.EXECUTION
            ])
            workflows = self.config.workflow.workflows

            # Filter to specific workflow if requested
            if workflow_filter and workflows:
                workflows = [w for w in workflows if w == workflow_filter or Path(w).name == workflow_filter]
                if not workflows:
                    raise TestError(f"Workflow not found: {workflow_filter}")
                self._log(f"Workflow filter: running only {workflows[0]}")

            if not needs_install and not workflows:
                # Only syntax was requested and no workflows
                self._log(f"\n{platform_name}: PASSED")
                return TestResult(platform_name, True)

            # Get platform provider
            platform = get_platform(platform_name, self._log)
            platform_config = self.config.get_platform_config(platform_name)

            # Create temporary work directory
            # ignore_cleanup_errors=True prevents WinError 32 when worker processes still have files open
            with tempfile.TemporaryDirectory(prefix="comfy_test_", ignore_cleanup_errors=True) as work_dir:
                work_path = Path(work_dir)

                # Enable isolation for Windows platforms (no Docker available)
                is_windows = platform_name in ("windows", "windows_portable")
                isolation = WindowsIsolation(work_path, self._log) if is_windows else None
                if isolation:
                    isolation.setup()

                # === INSTALL LEVEL ===
                # Always run install if any later level needs it
                self._log_level_start(TestLevel.INSTALL, TestLevel.INSTALL in requested_levels)

                if skip_setup:
                    # Skip setup: use existing ComfyUI with node already installed
                    if not comfyui_dir:
                        raise TestError(
                            "comfyui_dir required when skip_setup=True",
                            "Use auto-detect or provide --comfyui-dir"
                        )
                    self._log(f"Using existing ComfyUI: {comfyui_dir}")
                    self._log("Node already installed, skipping installation")
                    comfyui_path = Path(comfyui_dir).resolve()

                    # For portable, find embedded Python
                    if platform_name == "windows_portable":
                        python_embeded = comfyui_path.parent / "python_embeded"
                        if not python_embeded.exists():
                            python_embeded = comfyui_path.parent.parent / "python_embeded"
                        python_exe = python_embeded / "python.exe"
                    else:
                        python_exe = Path(sys.executable)

                    paths = TestPaths(
                        work_dir=work_path,
                        comfyui_dir=comfyui_path,
                        python=python_exe,
                        custom_nodes_dir=comfyui_path / "custom_nodes",
                    )
                elif comfyui_dir:
                    # Use existing ComfyUI but still install node
                    self._log(f"Using existing ComfyUI: {comfyui_dir}")
                    comfyui_path = Path(comfyui_dir).resolve()

                    if platform_name == "windows_portable":
                        python_embeded = comfyui_path.parent / "python_embeded"
                        if not python_embeded.exists():
                            python_embeded = comfyui_path.parent.parent / "python_embeded"
                        python_exe = python_embeded / "python.exe"
                    else:
                        python_exe = Path(sys.executable)

                    paths = TestPaths(
                        work_dir=work_path,
                        comfyui_dir=comfyui_path,
                        python=python_exe,
                        custom_nodes_dir=comfyui_path / "custom_nodes",
                    )

                    self._log("Installing custom node...")
                    platform.install_node(paths, self.node_dir)

                    node_reqs = get_node_reqs(self.node_dir)
                    if node_reqs:
                        self._log(f"Installing {len(node_reqs)} node dependency(ies)...")
                        for name, repo in node_reqs:
                            self._log(f"  {name} from {repo}")
                            platform.install_node_from_repo(paths, repo, name)
                else:
                    # Full setup: clone ComfyUI and install node
                    self._log("Setting up ComfyUI...")
                    paths = platform.setup_comfyui(self.config, work_path)

                    self._log("Installing custom node...")
                    platform.install_node(paths, self.node_dir)

                    node_reqs = get_node_reqs(self.node_dir)
                    if node_reqs:
                        self._log(f"Installing {len(node_reqs)} node dependency(ies)...")
                        for name, repo in node_reqs:
                            self._log(f"  {name} from {repo}")
                            platform.install_node_from_repo(paths, repo, name)

                # Install comfy-test's validation endpoint (always needed for VALIDATION level)
                self._log("Installing validation endpoint...")
                platform.install_node_from_repo(
                    paths,
                    "PozzettiAndrea/ComfyUI-validate-endpoint",
                    "ComfyUI-validate-endpoint"
                )

                self._log_level_done(TestLevel.INSTALL, "PASSED")

                # Check if we need server for remaining levels
                needs_server = any(l in config_levels for l in [
                    TestLevel.REGISTRATION, TestLevel.INSTANTIATION,
                    TestLevel.VALIDATION, TestLevel.EXECUTION
                ])

                if not needs_server:
                    self._log(f"\n{platform_name}: PASSED")
                    return TestResult(platform_name, True)

                # Get CUDA packages to mock from comfy-env.toml
                cuda_packages = get_cuda_packages(self.node_dir)
                # Skip mocking if real GPU available (COMFY_TEST_GPU=1)
                gpu_mode = os.environ.get("COMFY_TEST_GPU")
                self._log(f"COMFY_TEST_GPU env var = {gpu_mode!r}")
                if gpu_mode:
                    self._log("GPU mode: using real CUDA (no mocking)")
                    cuda_packages = []
                elif cuda_packages:
                    self._log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

                # Get env_vars from comfy-env.toml (CI only)
                env_vars = get_env_vars(self.node_dir)
                if env_vars:
                    self._log(f"Applying env_vars from comfy-env.toml: {', '.join(f'{k}={v}' for k, v in env_vars.items())}")

                # === Start server for remaining levels ===
                # Node discovery happens via ComfyUI's own loading mechanism
                if server_url:
                    self._log(f"\nConnecting to existing server at {server_url}...")
                    server_instance = ExternalComfyUIServer(server_url, log_callback=self._log)
                else:
                    self._log("\nStarting ComfyUI server...")
                    server_instance = ComfyUIServer(
                        platform, paths, self.config,
                        cuda_mock_packages=cuda_packages,
                        log_callback=self._log,
                        env_vars=env_vars,
                    )

                with server_instance as server:
                    api = server.get_api()

                    # === REGISTRATION LEVEL ===
                    # Check server startup logs for import errors
                    if TestLevel.REGISTRATION not in config_levels:
                        self._log_level_skip(TestLevel.REGISTRATION)
                    else:
                        self._log_level_start(TestLevel.REGISTRATION, TestLevel.REGISTRATION in requested_levels)
                        self._log("Checking for import errors in server logs...")
                        import_errors = server.get_import_errors()
                        if import_errors:
                            error_msg = "\n".join(import_errors)
                            raise TestError(
                                f"Node import failed ({len(import_errors)} error(s))",
                                error_msg
                            )
                        self._log("No import errors detected")
                        self._log_level_done(TestLevel.REGISTRATION, "PASSED")

                    # Get registered nodes from object_info for remaining tests
                    object_info = api.get_object_info()
                    registered_nodes = list(object_info.keys())
                    self._log(f"Found {len(registered_nodes)} registered nodes")

                    # === INSTANTIATION LEVEL ===
                    if TestLevel.INSTANTIATION not in config_levels:
                        self._log_level_skip(TestLevel.INSTANTIATION)
                    else:
                        self._log_level_start(TestLevel.INSTANTIATION, TestLevel.INSTANTIATION in requested_levels)
                        self._log("Testing node constructors...")
                        self._test_instantiation(platform, paths, registered_nodes, cuda_packages)
                        self._log(f"All {len(registered_nodes)} node(s) instantiated successfully!")
                        self._log_level_done(TestLevel.INSTANTIATION, "PASSED")

                    # === STATIC_CAPTURE LEVEL ===
                    if TestLevel.STATIC_CAPTURE not in config_levels:
                        self._log_level_skip(TestLevel.STATIC_CAPTURE)
                    elif not self.config.workflow.workflows:
                        self._log_level_start(TestLevel.STATIC_CAPTURE, TestLevel.STATIC_CAPTURE in requested_levels)
                        self._log("No workflows configured for static capture")
                        self._log_level_done(TestLevel.STATIC_CAPTURE, "PASSED (no workflows)")
                    else:
                        self._log_level_start(TestLevel.STATIC_CAPTURE, TestLevel.STATIC_CAPTURE in requested_levels)
                        total_screenshots = len(self.config.workflow.workflows)
                        self._log(f"Capturing {total_screenshots} static screenshot(s)...")

                        try:
                            from ..reporting.screenshot import WorkflowScreenshot, check_dependencies, ensure_dependencies
                            # Auto-install playwright if missing
                            if not ensure_dependencies(log_callback=self._log):
                                raise ImportError("Failed to install screenshot dependencies")
                            check_dependencies()

                            screenshots_dir = self._get_output_base() / "screenshots"
                            screenshots_dir.mkdir(parents=True, exist_ok=True)

                            ws = WorkflowScreenshot(server.base_url, log_callback=self._log)
                            ws.start()
                            try:
                                for idx, workflow_file in enumerate(self.config.workflow.workflows, 1):
                                    self._log(f"  [{idx}/{total_screenshots}] STATIC {workflow_file.name}")
                                    output_path = screenshots_dir / f"{workflow_file.stem}.png"
                                    ws.capture(self._resolve_workflow_path(workflow_file), output_path=output_path)
                            finally:
                                ws.stop()

                            self._log_level_done(TestLevel.STATIC_CAPTURE, "PASSED")
                        except ImportError:
                            self._log("WARNING: Screenshots disabled (playwright not installed)")
                            self._log_level_done(TestLevel.STATIC_CAPTURE, "SKIPPED (no playwright)")

                    # === VALIDATION LEVEL ===
                    if TestLevel.VALIDATION not in config_levels:
                        self._log_level_skip(TestLevel.VALIDATION)
                    elif not self.config.workflow.workflows:
                        self._log_level_start(TestLevel.VALIDATION, TestLevel.VALIDATION in requested_levels)
                        self._log("No workflows to validate")
                        self._log_level_done(TestLevel.VALIDATION, "PASSED (no workflows)")
                    else:
                        self._log_level_start(TestLevel.VALIDATION, TestLevel.VALIDATION in requested_levels)
                        total_workflows = len(self.config.workflow.workflows)
                        self._log(f"Validating {total_workflows} workflow(s)...")

                        try:
                            from ..reporting.screenshot import WorkflowScreenshot, check_dependencies, ensure_dependencies
                            # Auto-install playwright if missing
                            if not ensure_dependencies(log_callback=self._log):
                                raise ImportError("Failed to install screenshot dependencies")
                            check_dependencies()

                            ws = WorkflowScreenshot(server.base_url, log_callback=self._log)
                            ws.start()
                            validation_errors = []
                            try:
                                for idx, workflow_file in enumerate(self.config.workflow.workflows, 1):
                                    self._log(f"  [{idx}/{total_workflows}] Validating {workflow_file.name}")
                                    try:
                                        ws.validate_workflow(self._resolve_workflow_path(workflow_file))
                                        self._log(f"    OK")
                                    except Exception as e:
                                        self._log(f"    FAILED: {e}")
                                        validation_errors.append((workflow_file.name, str(e)))
                            finally:
                                ws.stop()

                            if validation_errors:
                                raise TestError(
                                    f"Workflow validation failed ({len(validation_errors)} error(s))",
                                    "\n".join(f"  - {name}: {err}" for name, err in validation_errors)
                                )
                            self._log_level_done(TestLevel.VALIDATION, "PASSED")
                        except ImportError:
                            self._log("WARNING: Validation requires playwright")
                            self._log_level_done(TestLevel.VALIDATION, "SKIPPED (no playwright)")

                    # === EXECUTION LEVEL ===
                    if TestLevel.EXECUTION not in config_levels:
                        self._log_level_skip(TestLevel.EXECUTION)
                    elif not self.config.workflow.workflows:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)
                        self._log("No workflows configured for execution")
                        self._log_level_done(TestLevel.EXECUTION, "PASSED (no workflows)")
                    elif platform_config.skip_workflow:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)
                        self._log("Skipped per platform config")
                        self._log_level_done(TestLevel.EXECUTION, "SKIPPED")
                    else:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)

                        # Check GPU availability for GPU-requiring workflows
                        gpu_available = has_gpu()
                        gpu_workflows = set(self.config.workflow.gpu or [])
                        if gpu_workflows:
                            if gpu_available:
                                self._log("GPU detected - will execute GPU workflows")
                            else:
                                self._log("No GPU detected - GPU workflows will be skipped")

                        total_workflows = len(self.config.workflow.workflows)
                        self._log(f"Running {total_workflows} workflow(s) (all with videos)...")

                        # Create a log capture wrapper that writes to both main log and current workflow log
                        current_workflow_log = []

                        def capture_log(msg):
                            # Don't call self._log here - server._log_all already does
                            current_workflow_log.append(msg)

                        # Always initialize browser for screenshots/videos
                        ws = None
                        screenshots_dir = None
                        videos_dir = None
                        try:
                            from ..reporting.screenshot import WorkflowScreenshot, check_dependencies, ensure_dependencies
                            # Auto-install playwright if missing
                            if not ensure_dependencies(log_callback=self._log):
                                raise ImportError("Failed to install screenshot dependencies")
                            check_dependencies()
                            ws = WorkflowScreenshot(server.base_url, log_callback=capture_log)
                            ws.start()
                            # Create screenshots and videos output directories
                            screenshots_dir = self._get_output_base() / "screenshots"
                            screenshots_dir.mkdir(parents=True, exist_ok=True)
                            videos_dir = self._get_output_base() / "videos"
                            videos_dir.mkdir(parents=True, exist_ok=True)
                        except ImportError:
                            self._log("WARNING: Screenshots disabled (playwright not installed)")

                        # Initialize results tracking
                        results = []
                        logs_dir = self._get_output_base() / "logs"
                        logs_dir.mkdir(parents=True, exist_ok=True)

                        # Get hardware info once for all workflows that run on this machine
                        hardware = get_hardware_info()

                        try:
                            runner = WorkflowRunner(api, capture_log)
                            all_errors = []
                            for idx, workflow_file in enumerate(self.config.workflow.workflows, 1):
                                # Clear execution cache before each workflow to prevent state accumulation
                                api.free_memory(unload_models=False)

                                # Reset workflow log for this workflow
                                current_workflow_log.clear()
                                # Register capture_log to receive [ComfyUI] output
                                server.add_log_listener(capture_log)
                                start_time = time.time()
                                status = "pass"
                                error_msg = None

                                # Check if this is a GPU workflow and we don't have GPU
                                is_gpu_workflow = workflow_file in gpu_workflows
                                if is_gpu_workflow and not gpu_available:
                                    self._log(f"  [{idx}/{total_workflows}] SKIPPED (GPU required) {workflow_file.name}")
                                    results.append({
                                        "name": workflow_file.stem,
                                        "status": "skipped",
                                        "duration_seconds": 0,
                                        "error": "GPU required but not available",
                                        "hardware": None,
                                    })
                                    continue

                                # Start progress spinner
                                spinner = ProgressSpinner(workflow_file.name, idx, total_workflows)
                                spinner.start()

                                # Start resource monitoring
                                is_gpu_test = os.environ.get("COMFY_TEST_GPU") == "1"
                                resource_monitor = ResourceMonitor(interval=1.0, monitor_gpu=is_gpu_test)
                                resource_monitor.start()

                                try:
                                    if ws and videos_dir:
                                        # Execute via browser + capture video frames (always)
                                        workflow_video_dir = videos_dir / workflow_file.stem
                                        final_screenshot_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                                        frames = ws.capture_execution_frames(
                                            self._resolve_workflow_path(workflow_file),
                                            output_dir=workflow_video_dir,
                                            log_lines=current_workflow_log,
                                            webp_quality=60,  # Low quality for video frames
                                            final_screenshot_path=final_screenshot_path,  # High quality PNG
                                            final_screenshot_delay_ms=5000,  # 5 second delay
                                        )
                                        capture_log(f"    Captured {len(frames)} video frames")
                                    else:
                                        # Execute via API only (fallback if no playwright)
                                        result = runner.run_workflow(
                                            workflow_file,
                                            timeout=get_workflow_timeout(self.config.workflow.timeout),
                                        )
                                        capture_log(f"    Status: {result.status}")
                                except (WorkflowError, TestTimeoutError, ScreenshotError) as e:
                                    status = "fail"
                                    # Include full error with details, not just message
                                    error_msg = str(e)
                                    capture_log(f"    Status: FAILED")
                                    capture_log(f"    Error: {e.message}")
                                    if e.details:
                                        capture_log(f"    Details: {e.details}")
                                    all_errors.append((workflow_file.name, str(e)))
                                finally:
                                    # Stop spinner with final status
                                    spinner.stop("PASS" if status == "pass" else "FAIL")
                                    # Remove listener so next workflow starts fresh
                                    server.remove_log_listener(capture_log)

                                duration = time.time() - start_time
                                resource_metrics = resource_monitor.stop()

                                # Save resource timeline to CSV (in logs/ folder which already exists)
                                if resource_metrics.get("timeline"):
                                    csv_path = logs_dir / f"{workflow_file.stem}_resources.csv"
                                    cpu_count = resource_metrics.get("cpu_count", 1)
                                    total_ram = resource_metrics.get("total_ram_gb", 16)
                                    with open(csv_path, 'w') as f:
                                        # Metadata in header comment for graph scaling
                                        f.write(f"# cpu_count={cpu_count},total_ram_gb={total_ram}\n")
                                        f.write("t,cpu_cores,ram_gb,gpu_pct\n")
                                        for sample in resource_metrics["timeline"]:
                                            gpu_val = sample['gpu'] if sample['gpu'] is not None else ''
                                            f.write(f"{sample['t']},{sample['cpu']},{sample['ram']},{gpu_val}\n")
                                    # Remove timeline from results.json (keep only summary stats)
                                    resource_metrics.pop("timeline", None)

                                results.append({
                                    "name": workflow_file.stem,
                                    "status": status,
                                    "duration_seconds": round(duration, 2),
                                    "error": error_msg,
                                    "hardware": hardware,
                                    "resources": resource_metrics,
                                })

                                # Save per-workflow log (copy the list since we clear it)
                                (logs_dir / f"{workflow_file.stem}.log").write_text("\n".join(current_workflow_log), encoding="utf-8")
                                # Save browser console logs
                                if ws:
                                    ws.save_console_logs(logs_dir / f"{workflow_file.stem}_console.log")
                                    ws.clear_console_logs()
                        finally:
                            if ws:
                                ws.stop()

                        # Save results.json
                        passed_count = sum(1 for r in results if r["status"] == "pass")
                        failed_count = sum(1 for r in results if r["status"] == "fail")
                        results_data = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "platform": platform_name,
                            "hardware": hardware,
                            "summary": {
                                "total": len(results),
                                "passed": passed_count,
                                "failed": failed_count
                            },
                            "workflows": results
                        }
                        results_file = self._get_output_base() / "results.json"
                        results_file.write_text(json.dumps(results_data, indent=2), encoding='utf-8')
                        self._log(f"Results saved to {results_file}")

                        # Generate HTML report
                        from ..reporting.html_report import generate_html_report
                        html_file = generate_html_report(self._get_output_base(), self.node_dir.name)
                        self._log(f"Saved: {html_file}")

                        if all_errors:
                            raise WorkflowExecutionError(
                                f"Workflow execution failed ({len(all_errors)} error(s))",
                                [f"{name}: {err}" for name, err in all_errors]
                            )
                        self._log_level_done(TestLevel.EXECUTION, "PASSED")

                # Teardown isolation on success
                if isolation:
                    isolation.teardown()

            self._log(f"\n{platform_name}: PASSED")
            return TestResult(platform_name, True)

        except TestError as e:
            # Teardown isolation on error
            if 'isolation' in locals() and isolation:
                isolation.teardown()
            self._log(f"\n{platform_name}: FAILED")
            self._log(f"Error: {e.message}")
            if e.details:
                self._log(f"Details: {e.details}")
            return TestResult(platform_name, False, str(e.message), e.details)

        except Exception as e:
            # Teardown isolation on error
            if 'isolation' in locals() and isolation:
                isolation.teardown()
            self._log(f"\n{platform_name}: FAILED (unexpected error)")
            self._log(f"Error: {e}")
            return TestResult(platform_name, False, str(e))

        finally:
            # ALWAYS save session log (even on failure/error)
            self._save_session_log()

    def _dry_run(self, platform_name: str, levels: List[TestLevel]) -> TestResult:
        """Show what would be done without doing it."""
        self._log("\n[DRY RUN] Would execute the following levels:\n")

        level_num = 0
        total = len([l for l in self.ALL_LEVELS if l in levels])

        for test_level in self.ALL_LEVELS:
            level_name = test_level.value.upper()
            if test_level in levels:
                level_num += 1
                self._log(f"[{level_num}/{total}] {level_name}")
                self._log("-" * 40)

                if test_level == TestLevel.SYNTAX:
                    self._log("  Check pyproject.toml vs requirements.txt")
                elif test_level == TestLevel.INSTALL:
                    self._log(f"  Setup ComfyUI ({self.config.comfyui_version})")
                    self._log(f"  Install node: {self.node_dir.name}")
                    self._log("  Install node dependencies (from comfy-env.toml)")
                elif test_level == TestLevel.REGISTRATION:
                    self._log("  Verify nodes in object_info")
                elif test_level == TestLevel.INSTANTIATION:
                    self._log("  Test node constructors")
                elif test_level == TestLevel.EXECUTION:
                    if self.config.workflow.workflows:
                        self._log(f"  Run {len(self.config.workflow.workflows)} workflow(s):")
                        for wf in self.config.workflow.workflows:
                            self._log(f"    - {wf}")
                    else:
                        self._log("  No workflows configured for execution")
                self._log("")
            else:
                self._log(f"[ ] {level_name}: SKIPPED\n")

        return TestResult(platform_name, True, details="Dry run")

    def _check_syntax(self) -> None:
        """Check project structure - pyproject.toml vs requirements.txt.

        Raises:
            TestError: If neither pyproject.toml nor requirements.txt exists
        """
        pyproject = self.node_dir / "pyproject.toml"
        requirements = self.node_dir / "requirements.txt"

        has_pyproject = pyproject.exists()
        has_requirements = requirements.exists()

        if has_pyproject:
            self._log("Found pyproject.toml (modern format)")
        if has_requirements:
            self._log("Found requirements.txt (legacy format)")

        if not has_pyproject and not has_requirements:
            raise TestError(
                "No dependency file found",
                "Expected pyproject.toml or requirements.txt in node directory"
            )

        # Check for problematic unicode characters in Python files
        self._check_unicode_characters()

    def _check_unicode_characters(self) -> None:
        """Check Python files for characters that can't encode on Windows (cp1252).

        Scans all .py files in the node directory for any characters that
        cannot be encoded in Windows cp1252 codepage. This catches:
        - Curly quotes (copy-pasted from documentation)
        - Emoji and symbols (checkmarks, warning signs, etc.)
        - Non-Latin characters

        Raises:
            TestError: If non-cp1252 characters are found
        """
        import unicodedata

        issues = []

        for py_file in self.node_dir.rglob("*.py"):
            # Skip common non-source directories
            rel_path = py_file.relative_to(self.node_dir)
            parts = rel_path.parts
            skip_dirs = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'site-packages', 'lib', 'Lib', '.pixi'}
            if any(p in skip_dirs or p.startswith('_env_') or p.startswith('.') for p in parts):
                continue

            try:
                content = py_file.read_text(encoding='utf-8')
            except UnicodeDecodeError as e:
                issues.append(f"{rel_path}: Failed to decode as UTF-8: {e}")
                continue

            file_issues = []
            for line_num, line in enumerate(content.splitlines(), 1):
                for col, char in enumerate(line, 1):
                    try:
                        char.encode('cp1252')
                    except UnicodeEncodeError:
                        char_name = unicodedata.name(char, f'U+{ord(char):04X}')
                        file_issues.append(
                            f"  Line {line_num}, col {col}: {char_name} ({repr(char)}) - not encodable in cp1252"
                        )

            if file_issues:
                issues.append(f"{rel_path}:\n" + "\n".join(file_issues))

        if issues:
            raise TestError(
                "Non-ASCII characters found that can't encode on Windows (cp1252)",
                "Replace with ASCII equivalents:\n\n" + "\n\n".join(issues)
            )

        self._log("Unicode check: OK (all characters cp1252-safe)")

    def _get_workflow_files(self) -> List[Path]:
        """Get workflow files configured for execution.

        Returns all discovered workflows from the workflows/ directory.

        Deprecated: Use config.workflow.workflows directly instead.
        """
        return self.config.workflow.workflows

    def _test_instantiation(
        self,
        platform,
        paths,
        registered_nodes: List[str],
        cuda_packages: List[str],
    ) -> None:
        """Test that all node constructors can be called without errors.

        This runs a subprocess that imports NODE_CLASS_MAPPINGS
        and calls each node's constructor.

        Args:
            platform: Platform provider
            paths: Test paths
            registered_nodes: List of registered node names (from object_info)
            cuda_packages: CUDA packages to mock

        Raises:
            TestError: If any node fails to instantiate
        """
        # Build the test script
        # Use proper package import by adding custom_nodes to sys.path
        script = '''
import sys
import os
import json
from pathlib import Path

# Disable CUDA to prevent crashes on CPU-only machines
# (model_management.py calls torch.cuda at import time)
os.environ["CUDA_VISIBLE_DEVICES"] = ""

# Mock CUDA packages if needed
cuda_packages = {cuda_packages_json}
for pkg in cuda_packages:
    if pkg not in sys.modules:
        import types
        import importlib.machinery
        mock_module = types.ModuleType(pkg)
        mock_module.__spec__ = importlib.machinery.ModuleSpec(pkg, None)
        sys.modules[pkg] = mock_module

# Import ComfyUI's folder_paths to set up paths
import folder_paths

# Add custom_nodes directory to sys.path for proper package imports
custom_nodes_dir = Path("{custom_nodes_dir}")
if str(custom_nodes_dir) not in sys.path:
    sys.path.insert(0, str(custom_nodes_dir))

# Import the node as a proper package
node_name = "{node_name}"
try:
    import importlib
    module = importlib.import_module(node_name)
except ImportError as e:
    print(json.dumps({{"success": False, "error": f"Failed to import {{node_name}}: {{e}}"}}))
    sys.exit(1)

# Get NODE_CLASS_MAPPINGS
mappings = getattr(module, "NODE_CLASS_MAPPINGS", {{}})

errors = []
instantiated = []

for name, cls in mappings.items():
    print(f"Instantiating: {{name}}", flush=True)
    try:
        instance = cls()
        instantiated.append(name)
        print(f"  OK: {{name}}", flush=True)
    except Exception as e:
        print(f"  FAILED: {{name}} - {{e}}", flush=True)
        errors.append({{"node": name, "error": str(e)}})

result = {{
    "success": len(errors) == 0,
    "instantiated": instantiated,
    "errors": errors,
}}
print(json.dumps(result))
'''.format(
            custom_nodes_dir=str(paths.custom_nodes_dir).replace("\\", "/"),
            node_name=self.node_dir.name,
            cuda_packages_json=json.dumps(cuda_packages),
        )

        # Run the script
        import subprocess

        result = subprocess.run(
            [str(paths.python), "-c", script],
            cwd=str(paths.comfyui_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise TestError(
                "Instantiation test failed",
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        try:
            # Extract JSON from stdout (may have log messages before it)
            stdout = result.stdout.strip()
            json_line = None
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    json_line = line
            if json_line is None:
                raise json.JSONDecodeError("No JSON found in output", stdout, 0)
            data = json.loads(json_line)
        except json.JSONDecodeError:
            raise TestError(
                "Instantiation test returned invalid JSON",
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        if not data.get("success"):
            error_details = "\n".join(
                f"  - {e['node']}: {e['error']}" for e in data.get("errors", [])
            )
            raise TestError(
                f"Node instantiation failed for {len(data.get('errors', []))} node(s)",
                error_details
            )

    def verify_only(self, platform_name: Optional[str] = None) -> List[TestResult]:
        """Verify node registration without running workflows.

        Args:
            platform_name: Specific platform, or None for current platform

        Returns:
            List of TestResult

        Note:
            This is equivalent to running with level=TestLevel.REGISTRATION
        """
        if platform_name is None:
            import sys
            if sys.platform == "linux":
                platform_name = "linux"
            elif sys.platform == "win32":
                platform_name = "windows"
            else:
                raise TestError(f"Unsupported platform: {sys.platform}")

        result = self.run_platform(platform_name, level=TestLevel.REGISTRATION)
        return [result]

    def _resolve_workflow_path(self, workflow_file: str) -> Path:
        """Resolve workflow file path relative to node directory.

        Args:
            workflow_file: Workflow filename or relative path

        Returns:
            Absolute Path to workflow file
        """
        workflow_path = Path(workflow_file)
        if not workflow_path.is_absolute():
            workflow_path = self.node_dir / workflow_file
        if not workflow_path.exists():
            # List what's in the node_dir
            self._log(f"  [DEBUG] Contents of {self.node_dir}:")
            for item in sorted(self.node_dir.iterdir()):
                self._log(f"  [DEBUG]   {item.name}{'/' if item.is_dir() else ''}")
            # Check if workflows dir exists
            workflows_dir = self.node_dir / "workflows"
            if workflows_dir.exists():
                self._log(f"  [DEBUG] Contents of {workflows_dir}:")
                for item in sorted(workflows_dir.iterdir()):
                    self._log(f"  [DEBUG]     {item.name}")
        return workflow_path
