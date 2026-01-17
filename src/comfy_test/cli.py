"""CLI for comfy-test."""

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .test.config import TestLevel
from .test.config_file import discover_config, load_config, CONFIG_FILE_NAMES
from .test.manager import TestManager
from .test.node_discovery import discover_nodes
from .errors import TestError, ConfigError, SetupError


def cmd_run(args) -> int:
    """Run installation tests."""
    try:
        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Parse level if specified
        level = None
        if args.level:
            level = TestLevel(args.level)

        # Create manager
        manager = TestManager(config)

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
        print(f"  CPU Only: {config.cpu_only}")
        print(f"  Timeout: {config.timeout}s")
        print(f"  Levels: {', '.join(l.value for l in config.levels)}")
        print()
        print("Platforms:")
        print(f"  Linux: {'enabled' if config.linux.enabled else 'disabled'}")
        print(f"  Windows: {'enabled' if config.windows.enabled else 'disabled'}")
        print(f"  Windows Portable: {'enabled' if config.windows_portable.enabled else 'disabled'}")
        print()
        print("Nodes (auto-discovered from NODE_CLASS_MAPPINGS):")
        try:
            node_dir = Path(args.config).parent if args.config else Path.cwd()
            nodes = discover_nodes(node_dir)
            print(f"  Found {len(nodes)} node(s):")
            for node in nodes:
                print(f"    - {node}")
        except SetupError as e:
            print(f"  Error discovering nodes: {e.message}")
        print()
        print("Workflows:")
        if config.workflow.files:
            print(f"  Files ({len(config.workflow.files)}):")
            for wf in config.workflow.files:
                print(f"    - {wf}")
            print(f"  Timeout: {config.workflow.timeout}s")
        else:
            print("  No workflows configured")

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
files = ["workflows/basic.json"]
timeout = 120
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
                capture_workflows,
                check_dependencies,
                ScreenshotError,
            )
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
        elif config and config.workflow.files:
            # Use workflows from config
            workflow_files = config.workflow.files
        else:
            # Auto-discover from workflows/ directory
            workflows_dir = node_dir / "workflows"
            if workflows_dir.exists():
                workflow_files = list(workflows_dir.glob("*.json"))

        if not workflow_files:
            print("No workflow files found.", file=sys.stderr)
            print("Specify a workflow file or configure workflows in comfy-test.toml", file=sys.stderr)
            return 1

        # Determine output directory
        output_dir = Path(args.output) if args.output else None

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
            print("Would capture screenshots for:")
            for wf in workflow_files:
                if output_dir:
                    out_path = output_dir / wf.with_suffix(".png").name
                else:
                    out_path = wf.with_suffix(".png")
                print(f"  {wf} -> {out_path}")
            if use_existing_server:
                print(f"Using existing server at: {server_url}")
            else:
                print("Would start ComfyUI server for screenshots")
            return 0

        # Log function
        def log(msg: str) -> None:
            print(msg)

        # Capture screenshots
        if use_existing_server:
            # Connect to existing server
            log(f"Connecting to existing server at {server_url}...")
            results = capture_workflows(
                workflow_files,
                output_dir=output_dir,
                server_url=server_url,
                log_callback=log,
            )
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
                    results = capture_workflows(
                        workflow_files,
                        output_dir=output_dir,
                        server_url=server.base_url,
                        log_callback=log,
                    )

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
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
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
    screenshot_parser.set_defaults(func=cmd_screenshot)

    parsed_args = parser.parse_args(args)
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
