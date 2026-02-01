"""Test manager for orchestrating installation tests."""

import faulthandler
import os
import time
from pathlib import Path
from typing import Optional, Callable, List

from ..common.config import TestConfig, TestLevel
from ..common.errors import TestError
from .context import LevelContext
from .results import TestResult
from .levels import (
    run_syntax,
    run_install,
    run_registration,
    run_instantiation,
    run_static_capture,
    run_validation,
    run_execution,
)


# Map test levels to their runner functions
LEVEL_RUNNERS = {
    TestLevel.SYNTAX: run_syntax,
    TestLevel.INSTALL: run_install,
    TestLevel.REGISTRATION: run_registration,
    TestLevel.INSTANTIATION: run_instantiation,
    TestLevel.STATIC_CAPTURE: run_static_capture,
    TestLevel.VALIDATION: run_validation,
    TestLevel.EXECUTION: run_execution,
}

# All levels in execution order
ALL_LEVELS = [
    TestLevel.SYNTAX,
    TestLevel.INSTALL,
    TestLevel.REGISTRATION,
    TestLevel.INSTANTIATION,
    TestLevel.STATIC_CAPTURE,
    TestLevel.VALIDATION,
    TestLevel.EXECUTION,
]


class TestManager:
    """Orchestrates installation tests across platforms.

    Args:
        config: Test configuration
        node_dir: Path to custom node directory (default: current directory)
        log_callback: Optional callback for logging
        output_dir: Optional output directory for results

    Example:
        >>> manager = TestManager(config)
        >>> results = manager.run_all()
        >>> for result in results:
        ...     print(f"{result.platform}: {'PASS' if result.success else 'FAIL'}")
    """

    def __init__(
        self,
        config: TestConfig,
        node_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.config = config
        self.node_dir = Path(node_dir) if node_dir else Path.cwd()
        self.output_dir = Path(output_dir) if output_dir else None
        self._original_log = log_callback or (
            lambda msg: print(
                msg.encode('ascii', errors='replace').decode('ascii')
                if isinstance(msg, str) else msg
            )
        )
        self._session_log: List[str] = []
        self._session_start_time: float = 0
        self._session_log_file: Optional[Path] = None
        self._level_index = 0
        self._total_levels = 0

    def _get_output_base(self) -> Path:
        """Get the base output directory for logs, screenshots, results."""
        return self.output_dir if self.output_dir else (self.node_dir / "comfy-test-results")

    def _log(self, msg: str) -> None:
        """Log message with timestamp, write to file immediately."""
        if self._session_start_time:
            elapsed = time.time() - self._session_start_time
            mins, secs = divmod(int(elapsed), 60)
            timestamp = f"[{mins:02d}:{secs:02d}]"
        else:
            timestamp = "[00:00]"

        timestamped_msg = f"{timestamp} {msg}"
        self._original_log(msg)
        self._session_log.append(timestamped_msg)

        if self._session_log_file:
            try:
                with open(self._session_log_file, "a", encoding="utf-8") as f:
                    f.write(timestamped_msg + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass

    def _save_session_log(self) -> None:
        """Log completion message."""
        if self._session_log_file and self._session_log_file.exists():
            self._original_log(f"Session log: {self._session_log_file}")

    def _log_level_start(self, level: TestLevel, in_config: bool) -> None:
        """Log the start of a test level."""
        self._level_index += 1
        level_name = level.value.upper()
        status = "" if in_config else " (implicit)"
        self._log("")
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
        server_url: Optional[str] = None,
        deps_installed: bool = False,
    ) -> List[TestResult]:
        """Run tests on all enabled platforms.

        Args:
            dry_run: If True, only show what would be done
            level: Maximum test level to run
            workflow_filter: If specified, only run this workflow
            comfyui_dir: Use existing ComfyUI directory
            server_url: If provided, connect to existing server
            deps_installed: If True, skip requirements.txt and install.py

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
                comfyui_dir=comfyui_dir,
                server_url=server_url,
                deps_installed=deps_installed,
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
        server_url: Optional[str] = None,
        work_dir: Optional[Path] = None,
        deps_installed: bool = False,
    ) -> TestResult:
        """Run tests on a specific platform.

        Args:
            platform_name: Platform to test
            dry_run: If True, only show what would be done
            level: Maximum test level to run
            workflow_filter: If specified, only run this workflow
            comfyui_dir: Use existing ComfyUI directory
            server_url: If provided, connect to existing server
            work_dir: Use this directory for work
            deps_installed: If True, skip requirements.txt and install.py

        Returns:
            TestResult for the platform
        """
        # Normalize platform name
        platform_name = platform_name.lower().replace("-", "_")

        # Determine which levels to run
        requested_levels = self.config.levels
        if level:
            max_idx = ALL_LEVELS.index(level)
            requested_levels = [l for l in requested_levels if ALL_LEVELS.index(l) <= max_idx]

        # Resolve dependencies
        config_levels = TestLevel.resolve_dependencies(requested_levels)

        # Skip INSTALL and SYNTAX levels when connecting to existing server
        if server_url:
            config_levels = [l for l in config_levels if l not in (TestLevel.SYNTAX, TestLevel.INSTALL)]

        # Calculate total levels for progress
        self._level_index = 0
        self._total_levels = len([l for l in ALL_LEVELS if l in config_levels])

        self._log(f"\n{'='*60}")
        self._log(f"Testing: {platform_name}")
        self._log(f"Levels: {', '.join(l.value for l in config_levels)}")
        # Log versions for debugging
        try:
            from importlib.metadata import version as get_version
            self._log(f"comfy-test: {get_version('comfy-test')}")
            try:
                self._log(f"comfy-env: {get_version('comfy-env')}")
            except Exception:
                self._log("comfy-env: not installed")
        except Exception:
            pass
        self._log(f"{'='*60}")

        if dry_run:
            return self._dry_run(platform_name, config_levels, requested_levels)

        # Initialize session
        self._session_log = []
        self._session_start_time = time.time()

        output_base = self._get_output_base()
        output_base.mkdir(parents=True, exist_ok=True)
        self._session_log_file = output_base / "session.log"
        self._session_log_file.write_text("", encoding="utf-8")

        # Enable crash dump logging
        crash_log_path = output_base / "crash_dump.log"
        crash_log_file = open(crash_log_path, "w")
        faulthandler.enable(file=crash_log_file)
        self._log(f"Crash dump logging enabled: {crash_log_path}")

        # Infer paths when using external server (INSTALL level is skipped)
        inferred_paths = None
        if server_url:
            from ..common.base_platform import TestPaths
            custom_nodes_dir = self.node_dir.parent
            inferred_comfyui_dir = comfyui_dir or custom_nodes_dir.parent
            # Try to find Python executable
            import sys
            python_path = Path(sys.executable)
            # On Windows portable, Python is in python_embeded/
            portable_python = inferred_comfyui_dir.parent / "python_embeded" / "python.exe"
            if portable_python.exists():
                python_path = portable_python
            inferred_paths = TestPaths(
                work_dir=output_base,
                comfyui_dir=inferred_comfyui_dir,
                python=python_path,
                custom_nodes_dir=custom_nodes_dir,
            )

        # Create initial context
        ctx = LevelContext(
            config=self.config,
            node_dir=self.node_dir,
            platform_name=platform_name,
            log=self._log,
            output_base=output_base,
            work_dir=work_dir,
            comfyui_dir=comfyui_dir,
            server_url=server_url,
            workflow_filter=workflow_filter,
            paths=inferred_paths,
            deps_installed=deps_installed,
        )

        try:
            # Run each level
            for test_level in ALL_LEVELS:
                if test_level not in config_levels:
                    continue

                self._log_level_start(test_level, test_level in requested_levels)

                runner = LEVEL_RUNNERS[test_level]
                ctx = runner(ctx)

                self._log_level_done(test_level, "PASSED")

            self._log(f"\n{platform_name}: PASSED")
            return TestResult(platform_name, True)

        except TestError as e:
            self._log(f"\n{platform_name}: FAILED")
            self._log(f"Error: {e.message}")
            if e.details:
                self._log(f"Details: {e.details}")
            return TestResult(platform_name, False, str(e.message), e.details)

        except Exception as e:
            self._log(f"\n{platform_name}: FAILED (unexpected error)")
            self._log(f"Error: {e}")
            return TestResult(platform_name, False, str(e))

        finally:
            # Cleanup
            if ctx.server:
                try:
                    ctx.server.stop()
                except Exception:
                    pass
            self._save_session_log()
            crash_log_file.close()

    def _dry_run(
        self,
        platform_name: str,
        levels: List[TestLevel],
        requested_levels: List[TestLevel],
    ) -> TestResult:
        """Show what would be done without doing it."""
        self._log("\n[DRY RUN] Would execute the following levels:\n")

        level_num = 0
        total = len([l for l in ALL_LEVELS if l in levels])

        for test_level in ALL_LEVELS:
            level_name = test_level.value.upper()
            if test_level in levels:
                level_num += 1
                self._log(f"[{level_num}/{total}] {level_name}")
                self._log("-" * 40)

                if test_level == TestLevel.SYNTAX:
                    self._log("  Check pyproject.toml vs requirements.txt")
                    self._log("  Check for non-CP1252 characters")
                elif test_level == TestLevel.INSTALL:
                    self._log(f"  Setup ComfyUI ({self.config.comfyui_version})")
                    self._log(f"  Install node: {self.node_dir.name}")
                    self._log("  Install node dependencies (from comfy-env.toml)")
                elif test_level == TestLevel.REGISTRATION:
                    self._log("  Start ComfyUI server")
                    self._log("  Check for import errors")
                    self._log("  Verify nodes in object_info")
                elif test_level == TestLevel.INSTANTIATION:
                    self._log("  Test node constructors")
                elif test_level == TestLevel.STATIC_CAPTURE:
                    if self.config.workflow.workflows:
                        self._log(f"  Capture {len(self.config.workflow.workflows)} static screenshot(s)")
                    else:
                        self._log("  No workflows configured")
                elif test_level == TestLevel.VALIDATION:
                    if self.config.workflow.workflows:
                        self._log(f"  Validate {len(self.config.workflow.workflows)} workflow(s)")
                    else:
                        self._log("  No workflows configured")
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
