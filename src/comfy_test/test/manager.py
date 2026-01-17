"""Test manager for orchestrating installation tests."""

import json
import tempfile
from pathlib import Path
from typing import Optional, Callable, List

from .config import TestConfig, TestLevel
from .comfy_env import get_cuda_packages, get_node_reqs
from .node_discovery import discover_nodes
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
                     TestLevel.INSTANTIATION, TestLevel.VALIDATION, TestLevel.EXECUTION]
            max_idx = order.index(level)
            requested_levels = [l for l in requested_levels if order.index(l) <= max_idx]

        # Resolve dependencies (e.g., validation needs install)
        config_levels = TestLevel.resolve_dependencies(requested_levels)

        self._log(f"\n{'='*60}")
        self._log(f"Testing on {platform_name}")
        self._log(f"Levels: {', '.join(l.value for l in config_levels)}")
        self._log(f"{'='*60}")

        if dry_run:
            return self._dry_run(platform_name, config_levels)

        try:
            # === SYNTAX LEVEL ===
            if TestLevel.SYNTAX not in config_levels:
                self._log("\n[syntax] Skipped")
            else:
                self._log("\n[syntax] Checking project structure...")
                self._check_syntax()

            # Check if we need install level for later levels
            needs_install = any(l in config_levels for l in [
                TestLevel.INSTALL, TestLevel.REGISTRATION,
                TestLevel.INSTANTIATION, TestLevel.VALIDATION
            ])
            run_workflows = self.config.workflow.files

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
                self._log("\n[install] Setting up ComfyUI...")
                paths = platform.setup_comfyui(self.config, work_path)

                self._log("[install] Installing custom node...")
                platform.install_node(paths, self.node_dir)

                node_reqs = get_node_reqs(self.node_dir)
                if node_reqs:
                    self._log(f"[install] Installing {len(node_reqs)} node dependency(ies)...")
                    for name, repo in node_reqs:
                        self._log(f"  {name} from {repo}")
                        platform.install_node_from_repo(paths, repo, name)
                else:
                    self._log("[install] No node dependencies to install")

                if TestLevel.INSTALL not in config_levels:
                    self._log("[install] (implicit - needed for later levels)")

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
                if cuda_packages:
                    self._log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

                # Discover nodes from NODE_CLASS_MAPPINGS before starting server
                expected_nodes = discover_nodes(self.node_dir)
                self._log(f"Discovered {len(expected_nodes)} node(s): {', '.join(expected_nodes)}")

                # === Start server for remaining levels ===
                self._log("\nStarting ComfyUI server...")
                with ComfyUIServer(
                    platform, paths, self.config,
                    cuda_mock_packages=cuda_packages,
                    log_callback=self._log,
                ) as server:
                    api = server.get_api()

                    # === REGISTRATION LEVEL ===
                    if TestLevel.REGISTRATION not in config_levels:
                        self._log("\n[registration] Skipped")
                    else:
                        self._log("\n[registration] Verifying node registration...")
                        api.verify_nodes(expected_nodes)
                        self._log(f"[registration] All {len(expected_nodes)} expected nodes found!")

                    # === INSTANTIATION LEVEL ===
                    if TestLevel.INSTANTIATION not in config_levels:
                        self._log("\n[instantiation] Skipped")
                    else:
                        self._log("\n[instantiation] Testing node constructors...")
                        self._test_instantiation(platform, paths, expected_nodes, cuda_packages)
                        self._log(f"[instantiation] All {len(expected_nodes)} node(s) instantiated successfully!")

                    # === VALIDATION LEVEL ===
                    if TestLevel.VALIDATION not in config_levels:
                        self._log("\n[validation] Skipped")
                    else:
                        workflow_files = self._get_workflow_files()

                        if workflow_files:
                            self._log(f"\n[validation] Validating {len(workflow_files)} workflow(s)...")
                            object_info = api.get_object_info()
                            validator = WorkflowValidator(
                                object_info,
                                cuda_packages=cuda_packages,
                                cuda_node_types=set(),
                            )

                            all_errors = []
                            for workflow_path in workflow_files:
                                self._log(f"  {workflow_path.name}:")
                                validation_result = validator.validate_file(workflow_path)

                                if not validation_result.is_valid:
                                    for err in validation_result.errors:
                                        self._log(f"    [ERROR] {err}")
                                        all_errors.append((workflow_path.name, err))
                                else:
                                    self._log(f"    Schema: OK")
                                    self._log(f"    Graph: OK")
                                    self._log(f"    Introspection: OK")

                                # Try partial execution of non-CUDA prefix
                                if validation_result.executable_nodes:
                                    with open(workflow_path) as f:
                                        workflow = json.load(f)
                                    exec_result = validator.execute_prefix(workflow, api, timeout=30)

                                    if exec_result.executed_nodes:
                                        self._log(f"    Execution: {len(exec_result.executed_nodes)} nodes executed")
                                    else:
                                        self._log(f"    Execution: OK (no non-CUDA nodes)")
                                    if exec_result.execution_errors:
                                        for node_id, error in exec_result.execution_errors.items():
                                            self._log(f"      [WARN] Node {node_id}: {error}")
                                else:
                                    self._log(f"    Execution: Skipped (all nodes require CUDA)")

                            if all_errors:
                                raise WorkflowValidationError(
                                    f"Workflow validation failed ({len(all_errors)} error(s))",
                                    [err for _, err in all_errors]
                                )
                            self._log(f"[validation] All {len(workflow_files)} workflow(s) validated!")
                        else:
                            self._log("\n[validation] No workflows to validate")

                    # === EXECUTION LEVEL ===
                    if TestLevel.EXECUTION not in config_levels:
                        self._log("\n[execution] Skipped")
                    elif not self.config.workflow.files:
                        self._log("\n[execution] No workflows configured")
                    elif platform_config.skip_workflow:
                        self._log("\n[execution] Skipped (platform config)")
                    else:
                        self._log(f"\n[execution] Running {len(self.config.workflow.files)} workflow(s)...")
                        runner = WorkflowRunner(api, self._log)
                        for workflow_file in self.config.workflow.files:
                            self._log(f"  Running: {workflow_file.name}")
                            result = runner.run_workflow(
                                workflow_file,
                                timeout=self.config.workflow.timeout,
                            )
                            self._log(f"    Status: {result['status']}")

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
        self._log("[DRY RUN] Would run:")

        if TestLevel.SYNTAX in levels:
            self._log(f"  [syntax] Check pyproject.toml vs requirements.txt")
        else:
            self._log(f"  [syntax] Skipped")

        if TestLevel.INSTALL in levels:
            self._log(f"  [install] Setup ComfyUI ({self.config.comfyui_version})")
            self._log(f"  [install] Install node: {self.node_dir.name}")
            self._log(f"  [install] Install node dependencies (from comfy-env.toml)")
        else:
            self._log(f"  [install] Skipped")

        if TestLevel.REGISTRATION in levels:
            self._log(f"  [registration] Verify nodes in object_info")
        else:
            self._log(f"  [registration] Skipped")

        if TestLevel.INSTANTIATION in levels:
            self._log(f"  [instantiation] Test node constructors")
        else:
            self._log(f"  [instantiation] Skipped")

        if TestLevel.VALIDATION in levels:
            self._log(f"  [validation] Validate workflows (schema + graph + types)")
        else:
            self._log(f"  [validation] Skipped")

        if TestLevel.EXECUTION in levels:
            if self.config.workflow.files:
                self._log(f"  [execution] Run {len(self.config.workflow.files)} workflow(s):")
                for wf in self.config.workflow.files:
                    self._log(f"    - {wf}")
            else:
                self._log(f"  [execution] No workflows configured")
        else:
            self._log(f"  [execution] Skipped")

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
            self._log("[syntax] Found pyproject.toml (modern format)")
        if has_requirements:
            self._log("[syntax] Found requirements.txt (legacy format)")

        if has_pyproject and has_requirements:
            self._log("[syntax] WARNING: Both pyproject.toml and requirements.txt exist")
            self._log("[syntax] Consider migrating fully to pyproject.toml")
        elif not has_pyproject and not has_requirements:
            raise TestError(
                "No dependency file found",
                "Expected pyproject.toml or requirements.txt in node directory"
            )
        elif has_requirements and not has_pyproject:
            self._log("[syntax] WARNING: Consider migrating to pyproject.toml")

        self._log("[syntax] OK")

    def _get_workflow_files(self) -> List[Path]:
        """Get workflow files to validate/run.

        Returns files from config, or auto-discovers from workflows/ directory.
        """
        # If config specifies files, use those
        if self.config.workflow.files:
            return self.config.workflow.files

        # Otherwise auto-discover from workflows/ directory
        workflows_dir = self.node_dir / "workflows"
        if workflows_dir.exists():
            return list(workflows_dir.glob("*.json"))
        return []

    def _test_instantiation(
        self,
        platform,
        paths,
        expected_nodes: List[str],
        cuda_packages: List[str],
    ) -> None:
        """Test that all node constructors can be called without errors.

        This runs a subprocess in the test venv that imports NODE_CLASS_MAPPINGS
        and calls each node's constructor.

        Args:
            platform: Platform provider
            paths: Test paths
            expected_nodes: List of expected node names
            cuda_packages: CUDA packages to mock

        Raises:
            TestError: If any node fails to instantiate
        """
        # Build the test script
        script = '''
import sys
import json

# Mock CUDA packages if needed
cuda_packages = {cuda_packages_json}
for pkg in cuda_packages:
    if pkg not in sys.modules:
        import types
        sys.modules[pkg] = types.ModuleType(pkg)

# Import ComfyUI's folder_paths to set up paths
import folder_paths

# Find and import the node module
import importlib.util
from pathlib import Path

node_dir = Path("{node_dir}")
init_file = node_dir / "__init__.py"

if not init_file.exists():
    print(json.dumps({{"success": False, "error": "No __init__.py found"}}))
    sys.exit(1)

spec = importlib.util.spec_from_file_location("test_node", init_file)
module = importlib.util.module_from_spec(spec)
sys.modules["test_node"] = module
spec.loader.exec_module(module)

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
            node_dir=str(paths.custom_nodes / self.node_dir.name),
            cuda_packages_json=json.dumps(cuda_packages),
        )

        # Run the script in the test venv
        import subprocess

        result = subprocess.run(
            [str(paths.python), "-c", script],
            cwd=str(paths.comfyui),
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
            data = json.loads(result.stdout.strip())
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
