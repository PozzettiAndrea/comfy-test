"""CLI for comfy-test."""

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .test.config import TestLevel
from .test.config_file import discover_config, load_config, CONFIG_FILE_NAMES
from .test.manager import TestManager
from .errors import TestError, ConfigError, SetupError


DEFAULT_CONFIG = """\
# comfy-test.toml - Test configuration for ComfyUI custom nodes
# Documentation: https://github.com/PozzettiAndrea/comfy-test

[test]
name = "{name}"
python_version = "3.11"
levels = ["syntax", "install", "registration"]

[test.workflows]
run = "all"        # or list specific files: ["workflow1.json", "workflow2.json"]
screenshot = "all"
timeout = 300

[test.platforms]
linux = true
windows = false
windows_portable = false
"""


def cmd_init(args) -> int:
    """Handle init command - create default comfy-test.toml."""
    config_path = Path.cwd() / "comfy-test.toml"

    if config_path.exists() and not args.force:
        print(f"Config file already exists: {config_path}", file=sys.stderr)
        print("Use --force to overwrite", file=sys.stderr)
        return 1

    # Try to auto-detect project name from folder
    name = Path.cwd().name

    content = DEFAULT_CONFIG.format(name=name)
    config_path.write_text(content)
    print(f"Created {config_path}")
    return 0


def cmd_run(args) -> int:
    """Run installation tests."""
    # Handle --local mode (run via act/Docker)
    if args.local:
        from .local_runner import run_local
        output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / ".comfy-test-output"
        return run_local(
            node_dir=Path.cwd(),
            output_dir=output_dir,
            config_file=args.config or "comfy-test.toml",
            gpu=args.gpu,
            log_callback=print,
        )

    try:
        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Create manager
        manager = TestManager(config)

        # Handle --only-level for single-level execution (multi-step CI)
        if args.only_level:
            only_level = TestLevel(args.only_level)
            work_dir = Path(args.work_dir) if args.work_dir else None

            if not args.platform:
                print("Error: --platform required with --only-level", file=sys.stderr)
                return 1

            result = manager.run_single_level(
                args.platform,
                only_level,
                work_dir=work_dir,
                skip_setup=args.skip_setup,
            )

            # Report result
            status = "PASS" if result.success else "FAIL"
            print(f"\n{'='*60}")
            print(f"RESULT: {status}")
            print(f"{'='*60}")

            if not result.success and result.error:
                print(f"Error: {result.error}")

            return 0 if result.success else 1

        # Standard multi-level execution
        # Parse level if specified (cumulative --level)
        level = None
        if args.level:
            level = TestLevel(args.level)

        # Run tests
        if args.platform:
            results = [manager.run_platform(args.platform, args.dry_run, level)]
        else:
            results = manager.run_all(args.dry_run, level)

        # Report results
        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")

        all_passed = True
        for result in results:
            status = "PASS" if result.success else "FAIL"
            print(f"  {result.platform}: {status}")
            if not result.success:
                all_passed = False
                if result.error:
                    print(f"    Error: {result.error}")

        return 0 if all_passed else 1

    except ConfigError as e:
        print(f"Configuration error: {e.message}", file=sys.stderr)
        if e.details:
            print(f"Details: {e.details}", file=sys.stderr)
        return 1
    except TestError as e:
        print(f"Test error: {e.message}", file=sys.stderr)
        return 1


def cmd_verify(args) -> int:
    """Verify node registration only."""
    try:
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        manager = TestManager(config)
        results = manager.verify_only(args.platform)

        all_passed = all(r.success for r in results)
        for result in results:
            status = "PASS" if result.success else "FAIL"
            print(f"{result.platform}: {status}")
            if not result.success and result.error:
                print(f"  Error: {result.error}")

        return 0 if all_passed else 1

    except (ConfigError, TestError) as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_info(args) -> int:
    """Show configuration and environment info."""
    try:
        if args.config:
            config = load_config(args.config)
            config_path = args.config
        else:
            try:
                config = discover_config()
                config_path = "auto-discovered"
            except ConfigError:
                print("No configuration file found.")
                print(f"Searched for: {', '.join(CONFIG_FILE_NAMES)}")
                return 1

        print(f"Configuration: {config_path}")
        print(f"  Name: {config.name}")
        print(f"  ComfyUI Version: {config.comfyui_version}")
        print(f"  Python Version: {config.python_version}")
        print(f"  Timeout: {config.timeout}s")
        print(f"  Levels: {', '.join(l.value for l in config.levels)}")
        print()
        print("Platforms:")
        print(f"  Linux: {'enabled' if config.linux.enabled else 'disabled'}")
        print(f"  Windows: {'enabled' if config.windows.enabled else 'disabled'}")
        print(f"  Windows Portable: {'enabled' if config.windows_portable.enabled else 'disabled'}")
        print()
        print("Nodes:")
        print("  Discovered at runtime when ComfyUI starts")
        print()
        print("Workflows:")
        print(f"  Timeout: {config.workflow.timeout}s")
        if config.workflow.run:
            print(f"  Run (execution): {len(config.workflow.run)} workflow(s)")
            for wf in config.workflow.run:
                print(f"    - {wf}")
        else:
            print("  Run (execution): none configured")
        if config.workflow.screenshot:
            print(f"  Screenshot: {len(config.workflow.screenshot)} workflow(s)")
            print("    (static_capture: static; execution: with outputs if also in run)")
            for wf in config.workflow.screenshot:
                print(f"    - {wf}")
        else:
            print("  Screenshot: none configured")

        return 0

    except ConfigError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1


def cmd_init_ci(args) -> int:
    """Generate GitHub Actions workflow file."""
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workflow_content = '''name: Test Installation
on: [push, pull_request]

jobs:
  test:
    uses: PozzettiAndrea/comfy-test/.github/workflows/test-matrix.yml@main
    with:
      config-file: "comfy-test.toml"
'''

    with open(output_path, "w") as f:
        f.write(workflow_content)

    print(f"Generated GitHub Actions workflow: {output_path}")
    print()
    print("Make sure to:")
    print("  1. Create a comfy-test.toml in your repository root")
    print("  2. Commit both files to your repository")
    print()
    print("Example comfy-test.toml:")
    print('''
[test]
name = "MyNode"
python_version = "3.10"

[test.workflows]
timeout = 120
run = ["basic.json"]  # Resolved from workflows/ folder
screenshot = ["basic.json"]
''')

    return 0


def cmd_download_portable(args) -> int:
    """Download ComfyUI Portable for testing."""
    from .test.platform.windows_portable import WindowsPortableTestPlatform

    platform = WindowsPortableTestPlatform()

    version = args.version
    if version == "latest":
        version = platform._get_latest_release_tag()

    output_path = Path(args.output)
    archive_path = output_path / f"ComfyUI_portable_{version}.7z"

    output_path.mkdir(parents=True, exist_ok=True)
    platform._download_portable(version, archive_path)

    print(f"Downloaded to: {archive_path}")
    return 0


def cmd_screenshot(args) -> int:
    """Generate workflow screenshots."""
    try:
        # Import screenshot module (requires optional dependencies)
        try:
            from .screenshot import (
                WorkflowScreenshot,
                check_dependencies,
                ScreenshotError,
            )
            from .screenshot_cache import ScreenshotCache
            check_dependencies()
        except ImportError as e:
            print(f"Error: {e}", file=sys.stderr)
            print("Install with: pip install comfy-test[screenshot]", file=sys.stderr)
            return 1

        # Load config to get workflow files
        if args.config:
            config = load_config(args.config)
            node_dir = Path(args.config).parent
        else:
            try:
                config = discover_config()
                node_dir = Path.cwd()
            except ConfigError:
                config = None
                node_dir = Path.cwd()

        # Determine which workflows to capture
        workflow_files = []

        if args.workflow:
            # Specific workflow provided
            workflow_path = Path(args.workflow)
            if not workflow_path.is_absolute():
                workflow_path = node_dir / workflow_path
            workflow_files = [workflow_path]
        elif config and config.workflow.screenshot:
            # Use workflows from config's screenshot list
            workflow_files = config.workflow.screenshot
        else:
            # Auto-discover from workflows/ directory
            workflows_dir = node_dir / "workflows"
            if workflows_dir.exists():
                workflow_files = sorted(workflows_dir.glob("*.json"))

        if not workflow_files:
            print("No workflow files found.", file=sys.stderr)
            print("Specify a workflow file or configure workflows in comfy-test.toml", file=sys.stderr)
            return 1

        # Determine output directory
        output_dir = Path(args.output) if args.output else None

        # Initialize cache
        cache = ScreenshotCache(node_dir)

        # Filter workflows that need updating (unless --force)
        def get_output_path(wf: Path) -> Path:
            if output_dir:
                if args.execute:
                    return output_dir / wf.with_stem(wf.stem + "_executed").with_suffix(".png").name
                return output_dir / wf.with_suffix(".png").name
            if args.execute:
                return wf.with_stem(wf.stem + "_executed").with_suffix(".png")
            return wf.with_suffix(".png")

        if args.force:
            workflows_to_capture = workflow_files
            skipped = []
        else:
            workflows_to_capture = []
            skipped = []
            for wf in workflow_files:
                out_path = get_output_path(wf)
                if cache.needs_update(wf, out_path):
                    workflows_to_capture.append(wf)
                else:
                    skipped.append(wf)

        # Determine server URL
        if args.server is True:
            # --server flag without URL, use default
            server_url = "http://localhost:8188"
            use_existing_server = True
        elif args.server:
            # --server with custom URL
            server_url = args.server
            use_existing_server = True
        else:
            # No --server flag, need to start our own server
            server_url = "http://127.0.0.1:8188"
            use_existing_server = False

        # Dry run mode
        if args.dry_run:
            if skipped:
                print(f"Skipping {len(skipped)} unchanged workflow(s):")
                for wf in skipped:
                    print(f"  {wf.name} (cached)")
            if workflows_to_capture:
                print(f"Would capture {len(workflows_to_capture)} screenshot(s):")
                for wf in workflows_to_capture:
                    out_path = get_output_path(wf)
                    print(f"  {wf} -> {out_path}")
            else:
                print("All screenshots up to date.")
            if use_existing_server and workflows_to_capture:
                print(f"Using existing server at: {server_url}")
            elif workflows_to_capture:
                print("Would start ComfyUI server for screenshots")
            return 0

        # Log function
        def log(msg: str) -> None:
            print(msg)

        # Report skipped workflows
        if skipped:
            log(f"Skipping {len(skipped)} unchanged workflow(s)")

        if not workflows_to_capture:
            log("All screenshots up to date.")
            return 0

        # Capture screenshots
        results = []

        if use_existing_server:
            # Connect to existing server
            log(f"Connecting to existing server at {server_url}...")
            with WorkflowScreenshot(server_url, log_callback=log) as ws:
                for wf in workflows_to_capture:
                    out_path = get_output_path(wf)
                    try:
                        if args.execute:
                            result = ws.capture_after_execution(
                                wf, out_path, timeout=args.timeout
                            )
                        else:
                            result = ws.capture(wf, out_path)
                        cache.save_fingerprint(wf, out_path)
                        results.append(result)
                    except ScreenshotError as e:
                        log(f"  ERROR: {e.message}")
        else:
            # Start our own server (requires full test environment)
            if not config:
                print("Error: No config file found.", file=sys.stderr)
                print("Use --server to connect to an existing ComfyUI server,", file=sys.stderr)
                print("or create a comfy-test.toml config file.", file=sys.stderr)
                return 1

            log("Setting up ComfyUI environment for screenshots...")
            from .test.platform import get_platform
            from .test.comfy_env import get_cuda_packages
            from .comfyui.server import ComfyUIServer

            platform = get_platform(log_callback=log)

            with tempfile.TemporaryDirectory(prefix="comfy_screenshot_") as work_dir:
                work_path = Path(work_dir)

                # Setup ComfyUI
                log("Setting up ComfyUI...")
                paths = platform.setup_comfyui(config, work_path)

                # Install the node
                log("Installing custom node...")
                platform.install_node(paths, node_dir)

                # Get CUDA packages to mock
                cuda_packages = get_cuda_packages(node_dir)

                # Start server
                log("Starting ComfyUI server...")
                with ComfyUIServer(
                    platform, paths, config,
                    cuda_mock_packages=cuda_packages,
                    log_callback=log,
                ) as server:
                    with WorkflowScreenshot(server.base_url, log_callback=log) as ws:
                        for wf in workflows_to_capture:
                            out_path = get_output_path(wf)
                            try:
                                if args.execute:
                                    result = ws.capture_after_execution(
                                        wf, out_path, timeout=args.timeout
                                    )
                                else:
                                    result = ws.capture(wf, out_path)
                                cache.save_fingerprint(wf, out_path)
                                results.append(result)
                            except ScreenshotError as e:
                                log(f"  ERROR: {e.message}")

        # Report results
        print(f"\nCaptured {len(results)} screenshot(s)")
        for path in results:
            print(f"  {path}")

        return 0

    except ScreenshotError as e:
        print(f"Screenshot error: {e.message}", file=sys.stderr)
        if e.details:
            print(f"Details: {e.details}", file=sys.stderr)
        return 1
    except (ConfigError, TestError) as e:
        print(f"Error: {e.message}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


def main(args=None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="comfy-test",
        description="Installation testing for ComfyUI custom nodes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run installation tests",
    )
    run_parser.add_argument(
        "--config", "-c",
        help="Path to config file (default: auto-discover)",
    )
    run_parser.add_argument(
        "--platform", "-p",
        choices=["linux", "windows", "windows-portable"],
        help="Run on specific platform only",
    )
    run_parser.add_argument(
        "--level", "-l",
        choices=["syntax", "install", "registration", "instantiation", "validation", "execution"],
        help="Run only up to this level (overrides config)",
    )
    run_parser.add_argument(
        "--only-level", "-L",
        choices=["syntax", "install", "registration", "instantiation", "validation", "execution"],
        help="Run ONLY this specific level (for multi-step CI)",
    )
    run_parser.add_argument(
        "--work-dir", "-w",
        help="Persistent work directory (for multi-step CI). State saved to work-dir/state.json",
    )
    run_parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip ComfyUI setup, load state from --work-dir (for resuming after install)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
    )
    run_parser.add_argument(
        "--local",
        action="store_true",
        help="Run tests locally via act (Docker) instead of directly",
    )
    run_parser.add_argument(
        "--output-dir", "-o",
        help="Output directory for screenshots/logs/results.json (with --local)",
    )
    run_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Enable GPU passthrough (with --local)",
    )
    run_parser.set_defaults(func=cmd_run)

    # verify command
    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify node registration only",
    )
    verify_parser.add_argument(
        "--config", "-c",
        help="Path to config file",
    )
    verify_parser.add_argument(
        "--platform", "-p",
        choices=["linux", "windows", "windows-portable"],
        help="Platform to verify on",
    )
    verify_parser.set_defaults(func=cmd_verify)

    # info command
    info_parser = subparsers.add_parser(
        "info",
        help="Show configuration info",
    )
    info_parser.add_argument(
        "--config", "-c",
        help="Path to config file",
    )
    info_parser.set_defaults(func=cmd_info)

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Create a default comfy-test.toml config file",
    )
    init_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing config file",
    )
    init_parser.set_defaults(func=cmd_init)

    # init-ci command
    init_ci_parser = subparsers.add_parser(
        "init-ci",
        help="Generate GitHub Actions workflow",
    )
    init_ci_parser.add_argument(
        "--output", "-o",
        default=".github/workflows/test-install.yml",
        help="Output file path",
    )
    init_ci_parser.set_defaults(func=cmd_init_ci)

    # download-portable command
    download_parser = subparsers.add_parser(
        "download-portable",
        help="Download ComfyUI Portable",
    )
    download_parser.add_argument(
        "--version", "-v",
        default="latest",
        help="Version to download (default: latest)",
    )
    download_parser.add_argument(
        "--output", "-o",
        default=".",
        help="Output directory",
    )
    download_parser.set_defaults(func=cmd_download_portable)

    # screenshot command
    screenshot_parser = subparsers.add_parser(
        "screenshot",
        help="Generate workflow screenshots with embedded metadata",
    )
    screenshot_parser.add_argument(
        "workflow",
        nargs="?",
        help="Specific workflow file to screenshot (default: all from config)",
    )
    screenshot_parser.add_argument(
        "--config", "-c",
        help="Path to config file",
    )
    screenshot_parser.add_argument(
        "--output", "-o",
        help="Output directory for screenshots (default: same as workflow)",
    )
    screenshot_parser.add_argument(
        "--server", "-s",
        nargs="?",
        const=True,
        default=False,
        help="Use existing ComfyUI server (default: localhost:8188, or specify URL)",
    )
    screenshot_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be captured without doing it",
    )
    screenshot_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force regeneration, ignoring cache",
    )
    screenshot_parser.add_argument(
        "--execute", "-e",
        action="store_true",
        help="Execute workflows before capturing (shows preview outputs)",
    )
    screenshot_parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=300,
        help="Execution timeout in seconds (default: 300, only used with --execute)",
    )
    screenshot_parser.set_defaults(func=cmd_screenshot)

    parsed_args = parser.parse_args(args)
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
