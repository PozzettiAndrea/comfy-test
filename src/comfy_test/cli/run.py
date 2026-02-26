"""Run command for comfy-test CLI."""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ..common.config import TestLevel
from ..common.config_file import discover_config, load_config
from ..common.errors import TestError, ConfigError
from .paths import are_paths_configured, run_setup_wizard, get_workspace_dir, get_logs_dir


def _safe_str(s) -> str:
    """Sanitize string for Windows cp1252 console encoding."""
    return str(s).encode('ascii', errors='replace').decode('ascii')


def get_current_platform() -> str:
    """Detect current OS and return matching platform name."""
    if sys.platform == "linux":
        return "linux"
    elif sys.platform == "darwin":
        return "macos"
    elif sys.platform == "win32":
        if "python_embeded" in sys.executable:
            return "windows_portable"
        return "windows"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def cmd_run(args) -> int:
    """Run tests in a fresh ComfyUI environment.

    1. Create workspace in configured workspace dir
    2. Clone ComfyUI and create venv
    3. Copy node into custom_nodes/
    4. Install node dependencies
    5. Run tests
    6. Output results to configured logs dir
    """
    from ..orchestration.manager import TestManager

    node_dir = Path.cwd()

    print(f"[comfy-test] Testing: {node_dir.name}")

    try:
        # Check if paths are configured
        if not are_paths_configured():
            run_setup_wizard()

        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Create workspace directory
        workspaces_dir = get_workspace_dir()
        workspaces_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%H%M")
        short_name = node_dir.name.removeprefix("ComfyUI-")
        work_dir = workspaces_dir / f"{short_name}-{timestamp}"
        if work_dir.exists():
            if not args.force:
                print(f"Workspace already exists: {work_dir}", file=sys.stderr)
                print("Use --force to overwrite.", file=sys.stderr)
                return 1
            shutil.rmtree(work_dir)
        work_dir.mkdir()

        print(f"[comfy-test] Workspace: {work_dir}")

        # Create output directory in logs dir
        logs_dir = get_logs_dir()
        logs_dir.mkdir(exist_ok=True)

        # Auto-detect platform if not specified
        platform = args.platform if args.platform else get_current_platform()

        # Handle --portable flag
        if args.portable:
            if platform not in ("windows", "windows_portable"):
                print("Error: --portable flag is only valid on Windows", file=sys.stderr)
                return 1
            platform = "windows_portable"

        # Build output path: logs_dir/NodeName-XXXX/branch/platform-gpu
        run_id = f"{short_name}-{timestamp}"
        branch = getattr(args, 'branch', None)
        gpu = args.gpu or os.environ.get("COMFY_TEST_GPU") == "1"
        gpu_suffix = "gpu" if gpu else "cpu"
        platform_dir = f"{platform}-{gpu_suffix}"
        if branch:
            output_dir = logs_dir / run_id / branch / platform_dir
        else:
            output_dir = logs_dir / run_id / platform_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        print(f"[comfy-test] Output: {output_dir}")
        print(f"[comfy-test] Platform: {platform}")

        # Create manager
        manager = TestManager(config, node_dir=node_dir, output_dir=output_dir)

        # Run tests
        level = TestLevel(args.level) if args.level else None
        workflow_filter = getattr(args, 'workflow', None)

        server_url = getattr(args, 'server_url', None)
        comfyui_dir = Path(args.comfyui_dir) if getattr(args, 'comfyui_dir', None) else None
        deps_installed = getattr(args, 'deps_installed', False)
        novram = getattr(args, 'novram', False)

        results = [manager.run_platform(
            platform,
            args.dry_run,
            level,
            workflow_filter,
            work_dir=work_dir,
            server_url=server_url,
            comfyui_dir=comfyui_dir,
            deps_installed=deps_installed,
            novram=novram,
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
                    print(f"    Error: {_safe_str(result.error)}")

        print(f"\nOutput: {output_dir}")
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
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
    )
    run_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Enable GPU mode (uses real CUDA instead of mocking)",
    )
    run_parser.add_argument(
        "--portable",
        action="store_true",
        help="Use Windows Portable mode (only valid on Windows)",
    )
    run_parser.add_argument(
        "--workflow", "-W",
        help="Run only this specific workflow",
    )
    run_parser.add_argument(
        "--server-url",
        help="Connect to existing ComfyUI server instead of starting one",
    )
    run_parser.add_argument(
        "--comfyui-dir",
        help="Use existing ComfyUI directory instead of cloning",
    )
    run_parser.add_argument(
        "--deps-installed",
        action="store_true",
        help="Skip requirements.txt and install.py (deps already installed)",
    )
    run_parser.add_argument(
        "--branch", "-b",
        help="Git branch name (adds branch folder to output path)",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing workspace directory",
    )
    run_parser.add_argument(
        "--novram",
        action="store_true",
        help="Pass --novram to ComfyUI (no VRAM reservation)",
    )
    run_parser.set_defaults(func=cmd_run)
