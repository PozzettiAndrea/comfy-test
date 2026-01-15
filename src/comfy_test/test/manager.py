"""Test manager for orchestrating installation tests."""

import tempfile
from pathlib import Path
from typing import Optional, Callable, List

from .config import TestConfig
from .comfy_env import get_cuda_packages
from .platform import get_platform, TestPlatform, TestPaths
from ..comfyui.server import ComfyUIServer
from ..comfyui.validator import WorkflowValidator
from ..comfyui.workflow import WorkflowRunner
from ..errors import TestError, VerificationError, WorkflowValidationError


class TestResult:
    """Result of a test run.

    Attributes:
        platform: Platform name
        success: Whether the test passed
        error: Error message if failed
        details: Additional details
    """

    def __init__(
        self,
        platform: str,
        success: bool,
        error: Optional[str] = None,
        details: Optional[str] = None,
    ):
        self.platform = platform
        self.success = success
        self.error = error
        self.details = details

    def __repr__(self) -> str:
        status = "PASS" if self.success else "FAIL"
        return f"TestResult({self.platform}: {status})"


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

    def __init__(
        self,
        config: TestConfig,
        node_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.node_dir = Path(node_dir) if node_dir else Path.cwd()
        self._log = log_callback or (lambda msg: print(msg))

    def run_all(self, dry_run: bool = False) -> List[TestResult]:
        """Run tests on all enabled platforms.

        Args:
            dry_run: If True, only show what would be done

        Returns:
            List of TestResult for each platform
        """
        results = []

        platforms = [
            ("linux", self.config.linux),
            ("windows", self.config.windows),
            ("windows_portable", self.config.windows_portable),
        ]

        for platform_name, platform_config in platforms:
            if not platform_config.enabled:
                self._log(f"Skipping {platform_name} (disabled)")
                continue

            result = self.run_platform(platform_name, dry_run)
            results.append(result)

        return results

    def run_platform(self, platform_name: str, dry_run: bool = False) -> TestResult:
        """Run tests on a specific platform.

        Args:
            platform_name: Platform to test ('linux', 'windows', 'windows_portable')
            dry_run: If True, only show what would be done

        Returns:
            TestResult for the platform
        """
        self._log(f"\n{'='*60}")
        self._log(f"Testing on {platform_name}")
        self._log(f"{'='*60}")

        if dry_run:
            self._log("[DRY RUN] Would run:")
            self._log(f"  1. Setup ComfyUI ({self.config.comfyui_version})")
            self._log(f"  2. Install node: {self.node_dir.name}")
            if self.config.expected_nodes:
                self._log(f"  3. Verify nodes: {', '.join(self.config.expected_nodes)}")
            if self.config.workflow.file:
                self._log(f"  4. Run workflow: {self.config.workflow.file}")
            return TestResult(platform_name, True, details="Dry run")

        try:
            # Get platform provider
            platform = get_platform(platform_name, self._log)
            platform_config = self.config.get_platform_config(platform_name)

            # Create temporary work directory
            with tempfile.TemporaryDirectory(prefix="comfy_test_") as work_dir:
                work_path = Path(work_dir)

                # Setup ComfyUI
                self._log("\n[Step 1/4] Setting up ComfyUI...")
                paths = platform.setup_comfyui(self.config, work_path)

                # Install custom node
                self._log("\n[Step 2/4] Installing custom node...")
                platform.install_node(paths, self.node_dir)

                # Get CUDA packages to mock from comfy-env.toml
                cuda_packages = get_cuda_packages(self.node_dir)
                if cuda_packages:
                    self._log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

                # Start server and verify
                self._log("\n[Step 3/4] Verifying node registration...")
                with ComfyUIServer(
                    platform, paths, self.config,
                    cuda_mock_packages=cuda_packages,
                    log_callback=self._log,
                ) as server:
                    api = server.get_api()

                    # Verify expected nodes
                    if self.config.expected_nodes:
                        api.verify_nodes(self.config.expected_nodes)
                        self._log(f"All {len(self.config.expected_nodes)} expected nodes found!")

                    # Validate workflow if configured
                    if self.config.workflow.file:
                        self._log("\n[Step 3b/4] Validating workflow...")
                        workflow_path = self._resolve_workflow_path(self.config.workflow.file)
                        object_info = api.get_object_info()
                        validator = WorkflowValidator(
                            object_info,
                            cuda_packages=cuda_packages,
                            cuda_node_types=set(),  # TODO: detect from comfy-env.toml
                        )
                        validation_result = validator.validate_file(workflow_path)

                        if not validation_result.is_valid:
                            for err in validation_result.errors:
                                self._log(f"  [ERROR] {err}")
                            raise WorkflowValidationError(
                                f"Workflow has {len(validation_result.errors)} validation error(s)",
                                validation_result.errors
                            )
                        self._log(f"Workflow validation passed (Level 1-3)!")

                        # Level 4: Try partial execution of non-CUDA prefix
                        if validation_result.executable_nodes:
                            self._log(f"\n[Step 3c/4] Partial execution ({len(validation_result.executable_nodes)} non-CUDA nodes)...")
                            import json
                            with open(workflow_path) as f:
                                workflow = json.load(f)
                            exec_result = validator.execute_prefix(workflow, api, timeout=30)

                            if exec_result.executed_nodes:
                                self._log(f"  Executed {len(exec_result.executed_nodes)} nodes successfully")
                            if exec_result.execution_errors:
                                for node_id, error in exec_result.execution_errors.items():
                                    self._log(f"  [WARN] Node {node_id} failed: {error}")
                            if exec_result.warnings:
                                for warn in exec_result.warnings:
                                    self._log(f"  [WARN] {warn}")

                    # Run workflow if configured and not skipped
                    if self.config.workflow.file and not platform_config.skip_workflow:
                        self._log("\n[Step 4/4] Running test workflow...")
                        runner = WorkflowRunner(api, self._log)
                        result = runner.run_workflow(
                            self.config.workflow.file,
                            timeout=self.config.workflow.timeout,
                        )
                        self._log(f"Workflow completed with status: {result['status']}")
                    else:
                        self._log("\n[Step 4/4] Skipping workflow (not configured or disabled)")

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

    def verify_only(self, platform_name: Optional[str] = None) -> List[TestResult]:
        """Verify node registration without running workflows.

        Args:
            platform_name: Specific platform, or None for current platform

        Returns:
            List of TestResult
        """
        if platform_name is None:
            import sys
            if sys.platform == "linux":
                platform_name = "linux"
            elif sys.platform == "win32":
                platform_name = "windows"
            else:
                raise TestError(f"Unsupported platform: {sys.platform}")

        # Temporarily disable workflow
        original_file = self.config.workflow.file
        self.config.workflow.file = None

        try:
            result = self.run_platform(platform_name)
            return [result]
        finally:
            self.config.workflow.file = original_file

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
        return workflow_path
