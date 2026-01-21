"""Test manager for orchestrating installation tests."""

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, List

from .config import TestConfig, TestLevel
from .comfy_env import get_cuda_packages, get_node_reqs
from .platform import get_platform, TestPlatform, TestPaths
from ..comfyui.server import ComfyUIServer
from ..comfyui.validator import WorkflowValidator
from ..comfyui.workflow import WorkflowRunner
from ..errors import TestError, VerificationError, WorkflowValidationError, WorkflowExecutionError, WorkflowError, TestTimeoutError
from ..screenshot import ScreenshotError


@dataclass
class TestState:
    """State persisted between CLI invocations for multi-step CI.

    This allows running test levels in separate CI steps while sharing
    the ComfyUI environment setup between them.
    """
    comfyui_dir: str
    python: str
    custom_nodes_dir: str
    cuda_packages: List[str]
    platform_name: str


def save_state(state: TestState, work_dir: Path) -> None:
    """Save test state to work directory for later resumption."""
    state_file = work_dir / "state.json"
    work_dir.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(asdict(state), f, indent=2)


def load_state(work_dir: Path) -> TestState:
    """Load test state from work directory."""
    state_file = work_dir / "state.json"
    if not state_file.exists():
        raise TestError(
            "No state file found",
            f"Expected {state_file}. Run install level first with --work-dir."
        )
    with open(state_file) as f:
        data = json.load(f)
    return TestState(**data)


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

    # All possible levels in order
    ALL_LEVELS = [
        TestLevel.SYNTAX, TestLevel.INSTALL, TestLevel.REGISTRATION,
        TestLevel.INSTANTIATION, TestLevel.EXECUTION
    ]

    def __init__(
        self,
        config: TestConfig,
        node_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.node_dir = Path(node_dir) if node_dir else Path.cwd()
        self._log = log_callback or (lambda msg: print(msg))
        self._level_index = 0
        self._total_levels = 0

    def _log_level_start(self, level: TestLevel, in_config: bool) -> None:
        """Log the start of a test level with clear formatting."""
        self._level_index += 1
        level_name = level.value.upper()
        status = "" if in_config else " (implicit)"
        self._log(f"\n[{self._level_index}/{self._total_levels}] {level_name}{status}")
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
    ) -> List[TestResult]:
        """Run tests on all enabled platforms.

        Args:
            dry_run: If True, only show what would be done
            level: Maximum test level to run (None = all levels + workflows)

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

            result = self.run_platform(platform_name, dry_run, level)
            results.append(result)

        return results

    def run_platform(
        self,
        platform_name: str,
        dry_run: bool = False,
        level: Optional[TestLevel] = None,
    ) -> TestResult:
        """Run tests on a specific platform.

        Args:
            platform_name: Platform to test ('linux', 'windows', 'windows_portable')
            dry_run: If True, only show what would be done
            level: Maximum test level to run (CLI override, None = use config levels)

        Returns:
            TestResult for the platform
        """
        # Determine which levels to run
        # If CLI --level specified, filter to only levels up to that point
        requested_levels = self.config.levels
        if level:
            order = [TestLevel.SYNTAX, TestLevel.INSTALL, TestLevel.REGISTRATION,
                     TestLevel.INSTANTIATION, TestLevel.EXECUTION]
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
                TestLevel.INSTANTIATION, TestLevel.EXECUTION
            ])
            run_workflows = self.config.workflow.run

            if not needs_install and not run_workflows:
                # Only syntax was requested and no workflows
                self._log(f"\n{platform_name}: PASSED")
                return TestResult(platform_name, True)

            # Get platform provider
            platform = get_platform(platform_name, self._log)
            platform_config = self.config.get_platform_config(platform_name)

            # Create temporary work directory
            with tempfile.TemporaryDirectory(prefix="comfy_test_") as work_dir:
                work_path = Path(work_dir)

                # === INSTALL LEVEL ===
                # Always run install if any later level needs it
                self._log_level_start(TestLevel.INSTALL, TestLevel.INSTALL in requested_levels)
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

                self._log_level_done(TestLevel.INSTALL, "PASSED")

                # Check if we need server for remaining levels
                needs_server = any(l in config_levels for l in [
                    TestLevel.REGISTRATION, TestLevel.INSTANTIATION,
                    TestLevel.EXECUTION
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

                # === Start server for remaining levels ===
                # Node discovery happens via ComfyUI's own loading mechanism
                self._log("\nStarting ComfyUI server...")
                with ComfyUIServer(
                    platform, paths, self.config,
                    cuda_mock_packages=cuda_packages,
                    log_callback=self._log,
                ) as server:
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

                    # === EXECUTION LEVEL ===
                    if TestLevel.EXECUTION not in config_levels:
                        self._log_level_skip(TestLevel.EXECUTION)
                    elif not self.config.workflow.run:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)
                        self._log("No workflows configured for execution")
                        self._log_level_done(TestLevel.EXECUTION, "PASSED (no workflows)")
                    elif platform_config.skip_workflow:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)
                        self._log("Skipped per platform config")
                        self._log_level_done(TestLevel.EXECUTION, "SKIPPED")
                    else:
                        self._log_level_start(TestLevel.EXECUTION, TestLevel.EXECUTION in requested_levels)
                        total_workflows = len(self.config.workflow.run)

                        # Determine which workflows need screenshots (smart execution mode)
                        screenshot_set = set(self.config.workflow.execution_screenshot or [])
                        screenshot_count = len([w for w in self.config.workflow.run if w in screenshot_set])

                        if screenshot_count:
                            self._log(f"Running {total_workflows} workflow(s) ({screenshot_count} with screenshots)...")
                        else:
                            self._log(f"Running {total_workflows} workflow(s)...")

                        # Create a log capture wrapper that writes to both main log and current workflow log
                        current_workflow_log = []

                        def capture_log(msg):
                            self._log(msg)
                            current_workflow_log.append(msg)

                        # Initialize browser only if any workflow needs screenshots
                        ws = None
                        screenshots_dir = None
                        if screenshot_set:
                            try:
                                from ..screenshot import WorkflowScreenshot, check_dependencies
                                check_dependencies()
                                ws = WorkflowScreenshot(server.base_url, log_callback=capture_log)
                                ws.start()
                                # Create screenshots output directory
                                screenshots_dir = self.node_dir / ".comfy-test" / "screenshots"
                                screenshots_dir.mkdir(parents=True, exist_ok=True)
                            except ImportError:
                                self._log("WARNING: Screenshots disabled (playwright not installed)")
                                screenshot_set = set()  # Disable screenshots

                        # Initialize results tracking
                        results = []
                        logs_dir = self.node_dir / ".comfy-test" / "logs"
                        logs_dir.mkdir(parents=True, exist_ok=True)

                        try:
                            runner = WorkflowRunner(api, capture_log)
                            all_errors = []
                            for idx, workflow_file in enumerate(self.config.workflow.run, 1):
                                # Reset workflow log for this workflow
                                current_workflow_log.clear()
                                start_time = time.time()
                                status = "pass"
                                error_msg = None

                                try:
                                    if workflow_file in screenshot_set and ws:
                                        # Execute via browser + capture screenshot
                                        capture_log(f"  [{idx}/{total_workflows}] RUNNING + SCREENSHOT {workflow_file.name}")
                                        output_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                                        ws.capture_after_execution(
                                            workflow_file,
                                            output_path=output_path,
                                            timeout=self.config.workflow.timeout,
                                        )
                                        capture_log(f"    Status: success")
                                    else:
                                        # Execute via API only (faster)
                                        capture_log(f"  [{idx}/{total_workflows}] RUNNING {workflow_file.name}")
                                        result = runner.run_workflow(
                                            workflow_file,
                                            timeout=self.config.workflow.timeout,
                                        )
                                        capture_log(f"    Status: {result['status']}")
                                except (WorkflowError, TestTimeoutError, ScreenshotError) as e:
                                    status = "fail"
                                    error_msg = str(e.message)
                                    capture_log(f"    Status: FAILED")
                                    capture_log(f"    Error: {e.message}")
                                    all_errors.append((workflow_file.name, str(e.message)))

                                duration = time.time() - start_time
                                results.append({
                                    "name": workflow_file.stem,
                                    "status": status,
                                    "duration_seconds": round(duration, 2),
                                    "error": error_msg
                                })

                                # Save per-workflow log (copy the list since we clear it)
                                (logs_dir / f"{workflow_file.stem}.log").write_text("\n".join(current_workflow_log))
                        finally:
                            if ws:
                                ws.stop()

                        # Save results.json
                        passed_count = sum(1 for r in results if r["status"] == "pass")
                        failed_count = sum(1 for r in results if r["status"] == "fail")
                        results_data = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "platform": platform_name,
                            "summary": {
                                "total": len(results),
                                "passed": passed_count,
                                "failed": failed_count
                            },
                            "workflows": results
                        }
                        results_file = self.node_dir / ".comfy-test" / "results.json"
                        results_file.write_text(json.dumps(results_data, indent=2))
                        self._log(f"  Results saved to {results_file}")

                        if all_errors:
                            raise WorkflowExecutionError(
                                f"Workflow execution failed ({len(all_errors)} error(s))",
                                [f"{name}: {err}" for name, err in all_errors]
                            )
                        self._log_level_done(TestLevel.EXECUTION, "PASSED")

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
                    if self.config.workflow.run:
                        self._log(f"  Run {len(self.config.workflow.run)} workflow(s):")
                        for wf in self.config.workflow.run:
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
        """Check Python files for problematic unicode characters.

        Scans all .py files in the node directory for characters that can
        cause issues on Windows, such as curly quotes (often copy-pasted
        from documentation).

        Raises:
            TestError: If problematic characters are found
        """
        # Problematic characters and their safe replacements
        problematic_chars = {
            '\u2018': "'",  # Left single quote
            '\u2019': "'",  # Right single quote
            '\u201c': '"',  # Left double quote
            '\u201d': '"',  # Right double quote
            '\u2013': '-',  # En dash
            '\u2014': '-',  # Em dash
            '\u2026': '...',  # Ellipsis
        }

        issues = []

        for py_file in self.node_dir.rglob("*.py"):
            # Skip common non-source directories
            rel_path = py_file.relative_to(self.node_dir)
            parts = rel_path.parts
            skip_dirs = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'site-packages', 'lib', 'Lib'}
            if any(p in skip_dirs or p.startswith('_env_') or p.startswith('.') for p in parts):
                continue

            try:
                content = py_file.read_text(encoding='utf-8')
            except UnicodeDecodeError as e:
                issues.append(f"{rel_path}: Failed to decode as UTF-8: {e}")
                continue

            file_issues = []
            for line_num, line in enumerate(content.splitlines(), 1):
                for char, replacement in problematic_chars.items():
                    if char in line:
                        col = line.index(char) + 1
                        char_name = {
                            '\u2018': 'left single quote',
                            '\u2019': 'right single quote',
                            '\u201c': 'left double quote',
                            '\u201d': 'right double quote',
                            '\u2013': 'en dash',
                            '\u2014': 'em dash',
                            '\u2026': 'ellipsis',
                        }.get(char, f'U+{ord(char):04X}')
                        file_issues.append(
                            f"  Line {line_num}, col {col}: {char_name} ({repr(char)}) - use {repr(replacement)}"
                        )

            if file_issues:
                issues.append(f"{rel_path}:\n" + "\n".join(file_issues))

        if issues:
            raise TestError(
                "Problematic unicode characters found in Python files",
                "These characters can cause issues on Windows:\n\n" + "\n\n".join(issues)
            )

        self._log("Unicode check: OK (no problematic characters found)")

    def _get_workflow_files(self) -> List[Path]:
        """Get workflow files configured for execution.

        Note: Validation always auto-discovers from workflows/ directory.
        This method returns files configured in config.workflow.run.

        Deprecated: Use config.workflow.run directly instead.
        """
        return self.config.workflow.run

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
import json
from pathlib import Path

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
    try:
        instance = cls()
        instantiated.append(name)
    except Exception as e:
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
        return workflow_path

    def run_single_level(
        self,
        platform_name: str,
        level: TestLevel,
        work_dir: Optional[Path] = None,
        skip_setup: bool = False,
    ) -> TestResult:
        """Run a single test level (for multi-step CI).

        This method is designed for CI workflows where each test level
        runs as a separate step. State is persisted between steps via
        the work_dir.

        Args:
            platform_name: Platform to test ('linux', 'windows', 'windows_portable')
            level: Specific level to run
            work_dir: Persistent directory for state (required for install and later levels)
            skip_setup: If True, load state from work_dir instead of running install

        Returns:
            TestResult for this level

        Example:
            # Step 1: Run syntax (no setup needed)
            manager.run_single_level('linux', TestLevel.SYNTAX)

            # Step 2: Run install (saves state)
            manager.run_single_level('linux', TestLevel.INSTALL, work_dir=Path('/tmp/ct'))

            # Step 3: Run registration (loads state)
            manager.run_single_level('linux', TestLevel.REGISTRATION,
                                     work_dir=Path('/tmp/ct'), skip_setup=True)
        """
        # Check if level is in config
        if level not in self.config.levels:
            self._log(f"[{level.value.upper()}] SKIPPED (not in config)")
            return TestResult(platform_name, True, details="Skipped - not in config")

        self._log(f"\n{'='*60}")
        self._log(f"Testing: {platform_name}")
        self._log(f"Level: {level.value}")
        self._log(f"{'='*60}")

        try:
            # === SYNTAX LEVEL ===
            if level == TestLevel.SYNTAX:
                self._log(f"\n[1/1] {level.value.upper()}")
                self._log("-" * 40)
                self._check_syntax()
                self._log(f"[{level.value.upper()}] PASSED")
                return TestResult(platform_name, True)

            # === INSTALL LEVEL ===
            if level == TestLevel.INSTALL:
                if not work_dir:
                    raise TestError(
                        "work_dir required for install level",
                        "Use --work-dir to specify a persistent directory"
                    )

                self._log(f"\n[1/1] {level.value.upper()}")
                self._log("-" * 40)

                platform = get_platform(platform_name, self._log)
                work_dir.mkdir(parents=True, exist_ok=True)

                self._log("Setting up ComfyUI...")
                paths = platform.setup_comfyui(self.config, work_dir)

                self._log("Installing custom node...")
                platform.install_node(paths, self.node_dir)

                node_reqs = get_node_reqs(self.node_dir)
                if node_reqs:
                    self._log(f"Installing {len(node_reqs)} node dependency(ies)...")
                    for name, repo in node_reqs:
                        self._log(f"  {name} from {repo}")
                        platform.install_node_from_repo(paths, repo, name)

                # Get CUDA packages for state
                cuda_packages = get_cuda_packages(self.node_dir)
                # Skip mocking if real GPU available (COMFY_TEST_GPU=1)
                gpu_mode = os.environ.get("COMFY_TEST_GPU")
                self._log(f"COMFY_TEST_GPU env var = {gpu_mode!r}")
                if gpu_mode:
                    self._log("GPU mode: using real CUDA (no mocking)")
                    cuda_packages = []
                elif cuda_packages:
                    self._log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

                # Save state for later levels
                state = TestState(
                    comfyui_dir=str(paths.comfyui_dir),
                    python=str(paths.python),
                    custom_nodes_dir=str(paths.custom_nodes_dir),
                    cuda_packages=cuda_packages,
                    platform_name=platform_name,
                )
                save_state(state, work_dir)
                self._log(f"State saved to {work_dir / 'state.json'}")

                self._log(f"[{level.value.upper()}] PASSED")
                return TestResult(platform_name, True)

            # === LEVELS REQUIRING SERVER (registration, instantiation, execution) ===
            if not work_dir:
                raise TestError(
                    "work_dir required",
                    "Use --work-dir to specify the directory containing state.json"
                )

            if not skip_setup:
                raise TestError(
                    "skip_setup required for levels after install",
                    "Use --skip-setup to load state from work_dir"
                )

            # Load state from previous install
            state = load_state(work_dir)

            # Skip mocking if real GPU available (COMFY_TEST_GPU=1)
            gpu_mode = os.environ.get("COMFY_TEST_GPU")
            self._log(f"COMFY_TEST_GPU env var = {gpu_mode!r}")
            if gpu_mode:
                self._log("GPU mode: using real CUDA (no mocking)")
                state.cuda_packages = []

            # Reconstruct paths from state
            paths = TestPaths(
                work_dir=work_dir,
                comfyui_dir=Path(state.comfyui_dir),
                python=Path(state.python),
                custom_nodes_dir=Path(state.custom_nodes_dir),
            )

            platform = get_platform(platform_name, self._log)
            platform_config = self.config.get_platform_config(platform_name)

            self._log(f"\n[1/1] {level.value.upper()}")
            self._log("-" * 40)

            # Start server for this level
            self._log("Starting ComfyUI server...")
            with ComfyUIServer(
                platform, paths, self.config,
                cuda_mock_packages=state.cuda_packages,
                log_callback=self._log,
            ) as server:
                api = server.get_api()

                if level == TestLevel.REGISTRATION:
                    self._log("Checking for import errors in server logs...")
                    import_errors = server.get_import_errors()
                    if import_errors:
                        error_msg = "\n".join(import_errors)
                        raise TestError(
                            f"Node import failed ({len(import_errors)} error(s))",
                            error_msg
                        )
                    self._log("No import errors detected")

                elif level == TestLevel.INSTANTIATION:
                    self._log("Testing node constructors...")
                    object_info = api.get_object_info()
                    registered_nodes = list(object_info.keys())
                    self._test_instantiation(platform, paths, registered_nodes, state.cuda_packages)
                    self._log(f"All {len(registered_nodes)} node(s) instantiated successfully!")

                elif level == TestLevel.EXECUTION:
                    if not self.config.workflow.run:
                        self._log("No workflows configured for execution")
                    elif platform_config.skip_workflow:
                        self._log("Skipped per platform config")
                    else:
                        total_workflows = len(self.config.workflow.run)

                        # Determine which workflows need screenshots (smart execution mode)
                        screenshot_set = set(self.config.workflow.execution_screenshot or [])
                        screenshot_count = len([w for w in self.config.workflow.run if w in screenshot_set])

                        if screenshot_count:
                            self._log(f"Running {total_workflows} workflow(s) ({screenshot_count} with screenshots)...")
                        else:
                            self._log(f"Running {total_workflows} workflow(s)...")

                        # Create a log capture wrapper that writes to both main log and current workflow log
                        current_workflow_log = []

                        def capture_log(msg):
                            self._log(msg)
                            current_workflow_log.append(msg)

                        # Initialize browser only if any workflow needs screenshots
                        ws = None
                        screenshots_dir = None
                        if screenshot_set:
                            try:
                                from ..screenshot import WorkflowScreenshot, check_dependencies
                                check_dependencies()
                                ws = WorkflowScreenshot(server.base_url, log_callback=capture_log)
                                ws.start()
                                # Create screenshots output directory
                                screenshots_dir = self.node_dir / ".comfy-test" / "screenshots"
                                screenshots_dir.mkdir(parents=True, exist_ok=True)
                            except ImportError:
                                self._log("WARNING: Screenshots disabled (playwright not installed)")
                                screenshot_set = set()  # Disable screenshots

                        # Initialize results tracking
                        results = []
                        logs_dir = self.node_dir / ".comfy-test" / "logs"
                        logs_dir.mkdir(parents=True, exist_ok=True)

                        try:
                            runner = WorkflowRunner(api, capture_log)
                            all_errors = []
                            for idx, workflow_file in enumerate(self.config.workflow.run, 1):
                                # Reset workflow log for this workflow
                                current_workflow_log.clear()
                                start_time = time.time()
                                status = "pass"
                                error_msg = None

                                try:
                                    if workflow_file in screenshot_set and ws:
                                        # Execute via browser + capture screenshot
                                        capture_log(f"  [{idx}/{total_workflows}] RUNNING + SCREENSHOT {workflow_file.name}")
                                        output_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                                        ws.capture_after_execution(
                                            workflow_file,
                                            output_path=output_path,
                                            timeout=self.config.workflow.timeout,
                                        )
                                        capture_log(f"    Status: success")
                                    else:
                                        # Execute via API only (faster)
                                        capture_log(f"  [{idx}/{total_workflows}] RUNNING {workflow_file.name}")
                                        result = runner.run_workflow(
                                            workflow_file,
                                            timeout=self.config.workflow.timeout,
                                        )
                                        capture_log(f"    Status: {result['status']}")
                                except (WorkflowError, TestTimeoutError, ScreenshotError) as e:
                                    status = "fail"
                                    error_msg = str(e.message)
                                    capture_log(f"    Status: FAILED")
                                    capture_log(f"    Error: {e.message}")
                                    all_errors.append((workflow_file.name, str(e.message)))

                                duration = time.time() - start_time
                                results.append({
                                    "name": workflow_file.stem,
                                    "status": status,
                                    "duration_seconds": round(duration, 2),
                                    "error": error_msg
                                })

                                # Save per-workflow log (copy the list since we clear it)
                                (logs_dir / f"{workflow_file.stem}.log").write_text("\n".join(current_workflow_log))
                        finally:
                            if ws:
                                ws.stop()

                        # Save results.json
                        passed_count = sum(1 for r in results if r["status"] == "pass")
                        failed_count = sum(1 for r in results if r["status"] == "fail")
                        results_data = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "platform": platform_name,
                            "summary": {
                                "total": len(results),
                                "passed": passed_count,
                                "failed": failed_count
                            },
                            "workflows": results
                        }
                        results_file = self.node_dir / ".comfy-test" / "results.json"
                        results_file.write_text(json.dumps(results_data, indent=2))
                        self._log(f"  Results saved to {results_file}")

                        if all_errors:
                            raise WorkflowExecutionError(
                                f"Workflow execution failed ({len(all_errors)} error(s))",
                                [f"{name}: {err}" for name, err in all_errors]
                            )

            self._log(f"[{level.value.upper()}] PASSED")
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
