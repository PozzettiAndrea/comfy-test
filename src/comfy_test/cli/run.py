"""Run command for comfy-test CLI."""

import sys
from pathlib import Path
from typing import Optional

from ..common.config import TestLevel
from ..common.config_file import discover_config, load_config
from ..common.errors import TestError, ConfigError


def get_current_platform() -> str:
    """Detect current OS and return matching platform name."""
    if sys.platform == "linux":
        return "linux"
    elif sys.platform == "darwin":
        return "macos"
    elif sys.platform == "win32":
        # Check if running from portable embedded Python
        if "python_embeded" in sys.executable:
            return "windows_portable"
        return "windows"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def detect_comfyui_parent(node_dir: Path) -> Optional[Path]:
    """Check if node_dir is inside a ComfyUI custom_nodes folder.

    Expected structure: .../ComfyUI/custom_nodes/MyNode

    Returns:
        Path to ComfyUI directory if detected, None otherwise.
    """
    parent = node_dir.parent  # custom_nodes
    if parent.name != "custom_nodes":
        return None

    comfyui = parent.parent  # ComfyUI
    # Verify it's actually ComfyUI
    if (comfyui / "main.py").exists() and (comfyui / "comfy").is_dir():
        return comfyui

    return None


def cmd_run(args) -> int:
    """Run tests.

    Two modes:
    1. Default: Auto-detect parent ComfyUI, skip setup, just run tests
    2. --clean: Clone fresh ComfyUI to temp dir, copy node, run tests, cleanup
    """
    from ..orchestration.manager import TestManager

    node_dir = Path.cwd()

    # Handle --clean mode: fresh ComfyUI clone
    if args.clean:
        return run_clean_test(node_dir, args)

    # Default: auto-detect parent ComfyUI
    comfyui_dir = detect_comfyui_parent(node_dir)
    if not comfyui_dir:
        print("Error: Not inside a ComfyUI custom_nodes folder.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Expected structure:", file=sys.stderr)
        print("  ~/ComfyUI/custom_nodes/YourNode/  <-- run from here", file=sys.stderr)
        print("", file=sys.stderr)
        print("Or use --clean to test in a fresh ComfyUI:", file=sys.stderr)
        print("  comfy-test run --clean", file=sys.stderr)
        return 1

    print(f"[comfy-test] Detected ComfyUI: {comfyui_dir}")
    print(f"[comfy-test] Testing node: {node_dir.name}")

    try:
        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Create manager
        output_dir = Path(args.output_dir) if args.output_dir else None
        manager = TestManager(config, node_dir=node_dir, output_dir=output_dir)

        # Run tests (skip_setup=True since we're inside existing ComfyUI)
        level = TestLevel(args.level) if args.level else None
        workflow_filter = getattr(args, 'workflow', None)

        server_url = getattr(args, 'server_url', None)

        # Auto-detect platform if not specified
        platform = args.platform if args.platform else get_current_platform()
        print(f"[comfy-test] Platform: {platform}")

        results = [manager.run_platform(
            platform,
            args.dry_run,
            level,
            workflow_filter,
            comfyui_dir=comfyui_dir,
            skip_setup=True,
            server_url=server_url,
        )]

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


def run_clean_test(node_dir: Path, args) -> int:
    """Run tests in a fresh ComfyUI environment.

    1. Clone ComfyUI to temp dir
    2. Copy node into custom_nodes/
    3. Install node dependencies
    4. Run tests
    5. Cleanup temp dir
    """
    import tempfile
    from ..orchestration.manager import TestManager

    print(f"[comfy-test] Clean mode: testing {node_dir.name}")

    try:
        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Create temp directory for fresh ComfyUI
        with tempfile.TemporaryDirectory(prefix="comfy_test_clean_", ignore_cleanup_errors=True) as temp_dir:
            print(f"[comfy-test] Work directory: {temp_dir}")

            # Create manager
            output_dir = Path(args.output_dir) if args.output_dir else None
            manager = TestManager(config, node_dir=node_dir, output_dir=output_dir)

            # Run tests (skip_setup=False to do full setup)
            level = TestLevel(args.level) if args.level else None
            workflow_filter = getattr(args, 'workflow', None)

            # Auto-detect platform if not specified
            platform = args.platform if args.platform else get_current_platform()
            print(f"[comfy-test] Platform: {platform}")

            results = [manager.run_platform(
                platform,
                args.dry_run,
                level,
                workflow_filter,
                skip_setup=False,
            )]

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


def add_run_parser(subparsers):
    """Add the run subcommand parser."""
    run_parser = subparsers.add_parser(
        "run",
        help="Run tests",
    )
    run_parser.add_argument(
        "--config", "-c",
        help="Path to config file (default: auto-discover)",
    )
    run_parser.add_argument(
        "--platform", "-p",
        choices=["linux", "macos", "windows", "windows-portable"],
        help="Run on specific platform only",
    )
    run_parser.add_argument(
        "--level", "-l",
        choices=["syntax", "install", "registration", "instantiation", "validation", "execution"],
        help="Run only up to this level (overrides config)",
    )
    run_parser.add_argument(
        "--clean",
        action="store_true",
        help="Clone fresh ComfyUI to temp dir, copy node, run tests, cleanup",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
    )
    run_parser.add_argument(
        "--output-dir", "-o",
        help="Output directory for screenshots/logs/results.json",
    )
    run_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Enable GPU mode (uses real CUDA instead of mocking)",
    )
    run_parser.add_argument(
        "--workflow", "-W",
        help="Run only this specific workflow",
    )
    run_parser.add_argument(
        "--server-url",
        help="Connect to existing ComfyUI server instead of starting one",
    )
    run_parser.set_defaults(func=cmd_run)
