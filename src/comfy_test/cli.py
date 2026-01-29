"""CLI for comfy-test."""

import argparse
import os
import shutil
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Optional

from .test.config import TestLevel
from .test.config_file import discover_config, load_config, CONFIG_FILE_NAMES
from .test.manager import TestManager
from .errors import TestError, ConfigError, SetupError


def cmd_init(args) -> int:
    """Handle init command - copy template files."""
    cwd = Path.cwd()
    config_path = cwd / "comfy-test.toml"
    github_dir = cwd / ".github"

    # Get templates directory from package
    templates = files("comfy_test") / "templates"

    # Check existing files
    if not args.force:
        if config_path.exists():
            print(f"Config file already exists: {config_path}", file=sys.stderr)
            print("Use --force to overwrite", file=sys.stderr)
            return 1

    # Copy comfy-test.toml
    template_config = templates / "comfy-test.toml"
    shutil.copy(template_config, config_path)
    print(f"Created {config_path}")

    # Copy github/ -> .github/
    template_github = templates / "github"
    if template_github.is_dir():
        shutil.copytree(template_github, github_dir, dirs_exist_ok=True)
        print(f"Created {github_dir}/")

    return 0


def cmd_run(args) -> int:
    """Run installation tests."""
    # Handle --local mode (run via act/Docker)
    if args.local:
        from .local_runner import run_local
        output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / ".comfy-test-logs"
        return run_local(
            node_dir=Path.cwd(),
            output_dir=output_dir,
            config_file=args.config or "comfy-test.toml",
            gpu=args.gpu,
            log_callback=print,
            platform_name=args.platform,  # Pass through platform (auto-detected if None)
        )

    # Auto-detect: if not in CI and not already in Docker, decide how to run
    in_ci = os.environ.get('GITHUB_ACTIONS') or os.environ.get('ACT')
    in_docker = os.environ.get('COMFY_TEST_IN_DOCKER')

    if not in_ci and not in_docker:
        # Check if Docker is available
        is_windows = sys.platform == "win32"
        docker_available = False

        if is_windows:
            import subprocess
            try:
                result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                docker_available = result.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                docker_available = False
        else:
            # On Linux, assume Docker works
            docker_available = True

        if docker_available:
            from .local_runner import run_local
            print("[comfy-test] Running locally via Docker...")
            output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / ".comfy-test-logs"
            return run_local(
                node_dir=Path.cwd(),
                output_dir=output_dir,
                config_file=args.config or "comfy-test.toml",
                gpu=args.gpu,
                log_callback=print,
                platform_name=args.platform,
            )
        # No Docker on Windows - fall through to run directly with isolation
        print("[comfy-test] Running directly (no Docker)...")

    try:
        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Create manager with output_dir if specified
        output_dir = Path(args.output_dir) if args.output_dir else None
        manager = TestManager(config, output_dir=output_dir)

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
        workflow_filter = getattr(args, 'workflow', None)
        comfyui_dir = Path(args.comfyui_dir) if args.comfyui_dir else None
        if args.platform:
            results = [manager.run_platform(args.platform, args.dry_run, level, workflow_filter, comfyui_dir=comfyui_dir)]
        else:
            results = manager.run_all(args.dry_run, level, workflow_filter, comfyui_dir=comfyui_dir)

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
        if config.workflow.workflows:
            print(f"  Discovered: {len(config.workflow.workflows)} workflow(s)")
            for wf in config.workflow.workflows:
                print(f"    - {wf.name}")
        else:
            print("  Discovered: none")
        if config.workflow.gpu:
            print(f"  GPU required: {len(config.workflow.gpu)} workflow(s)")
            for wf in config.workflow.gpu:
                print(f"    - {wf.name}")

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


def cmd_build_windows_image(args) -> int:
    """Build the Windows base image for faster local testing."""
    import platform as plat
    if plat.system() != "Windows":
        print("Error: This command only works on Windows", file=sys.stderr)
        return 1

    from .local_runner import build_windows_base_image

    try:
        image_name = build_windows_base_image(print, force=args.rebuild)
        print(f"\nBase image ready: {image_name}")
        print("Subsequent 'ct test' runs will use this image for fast startup.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_pool(args) -> int:
    """Manage container pool for faster Linux testing."""
    from .container_pool import ContainerPool

    if not args.pool_cmd:
        print("Usage: ct pool {start|stop|status}", file=sys.stderr)
        return 1

    size = getattr(args, 'size', 2)
    pool = ContainerPool(size=size)

    if args.pool_cmd == "start":
        pool.start()
    elif args.pool_cmd == "stop":
        pool.stop()
    elif args.pool_cmd == "status":
        status = pool.status()
        print(f"Pool: {status['ready']}/{status['target_size']} ready")
        if status['containers']:
            for cid in status['containers']:
                print(f"  - {cid}")
        else:
            print("  (no containers)")
    return 0


def cmd_merge(args) -> int:
    """Merge results with existing gh-pages."""
    from .merge import merge_with_gh_pages

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Output directory not found: {output_dir}", file=sys.stderr)
        return 1

    success = merge_with_gh_pages(output_dir, args.repo, log_callback=print)
    return 0 if success else 1


def cmd_generate_index(args) -> int:
    """Generate index.html with platform tabs for a single branch/directory."""
    from .report import generate_root_index

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Output directory not found: {output_dir}", file=sys.stderr)
        return 1

    index_file = generate_root_index(output_dir, args.repo_name)
    print(f"Generated: {index_file}")
    return 0


def cmd_generate_root_index(args) -> int:
    """Generate root index.html with branch switcher tabs."""
    from .report import generate_branch_root_index

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Output directory not found: {output_dir}", file=sys.stderr)
        return 1

    index_file = generate_branch_root_index(output_dir, args.repo_name)
    print(f"Generated: {index_file}")
    return 0


def cmd_publish(args) -> int:
    """Publish results to gh-pages."""
    import shutil
    import subprocess
    import tempfile
    from .merge import merge_with_gh_pages
    from .report import generate_html_report

    results_dir = Path(args.results_dir).expanduser()
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}", file=sys.stderr)
        return 1

    results_file = results_dir / "results.json"
    if not results_file.exists():
        print(f"No results.json found in {results_dir}", file=sys.stderr)
        return 1

    repo = args.repo

    # Merge if requested (for CPU-only CI runs to preserve GPU results)
    if args.merge:
        print("Merging with existing gh-pages results...")
        merge_with_gh_pages(results_dir, repo, log_callback=print)

    # Generate HTML report
    print("Generating HTML report...")
    generate_html_report(results_dir)

    # Push to gh-pages
    print(f"Publishing to gh-pages for {repo}...")

    with tempfile.TemporaryDirectory() as tmp:
        gh_pages_dir = Path(tmp) / "gh-pages"

        # Try to clone existing gh-pages branch
        clone_result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch=gh-pages",
             f"https://github.com/{repo}.git", str(gh_pages_dir)],
            capture_output=True
        )

        if clone_result.returncode != 0:
            # No gh-pages branch exists, create empty dir
            print("No existing gh-pages branch, creating new one...")
            gh_pages_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=gh_pages_dir, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "gh-pages"], cwd=gh_pages_dir, capture_output=True)

        # Clear old content (except .git)
        for item in gh_pages_dir.iterdir():
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # Copy new content
        files_to_copy = ["results.json", "index.html"]
        dirs_to_copy = ["screenshots", "videos", "logs"]

        for f in files_to_copy:
            src = results_dir / f
            if src.exists():
                shutil.copy2(src, gh_pages_dir / f)

        for d in dirs_to_copy:
            src = results_dir / d
            if src.exists():
                shutil.copytree(src, gh_pages_dir / d)

        # Commit and push
        subprocess.run(["git", "add", "-A"], cwd=gh_pages_dir, check=True)

        # Check if there are changes to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=gh_pages_dir, capture_output=True, text=True
        )
        if not status.stdout.strip():
            print("No changes to publish")
            return 0

        subprocess.run(
            ["git", "commit", "-m", "Update test results"],
            cwd=gh_pages_dir, check=True
        )

        # Push (requires auth - user should have git credentials configured)
        push_result = subprocess.run(
            ["git", "push", "-f", f"https://github.com/{repo}.git", "gh-pages"],
            cwd=gh_pages_dir
        )

        if push_result.returncode != 0:
            print("Push failed. Make sure you have write access to the repo.")
            print("You may need to set up a GitHub token:")
            print("  git config --global credential.helper store")
            print("  # Then push manually once to save credentials")
            return 1

    print(f"Published to https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/")
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
        elif config and config.workflow.workflows:
            # Use workflows from config
            workflow_files = config.workflow.workflows
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
        choices=["linux", "macos", "windows", "windows-portable"],
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
        "--comfyui-dir",
        help="Use existing ComfyUI directory instead of cloning (skips clone + requirements install)",
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
        help="Output directory for screenshots/logs/results.json",
    )
    run_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Enable GPU passthrough (with --local)",
    )
    run_parser.add_argument(
        "--workflow", "-W",
        help="Run only this specific workflow (e.g., fix_normals.json)",
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
        choices=["linux", "macos", "windows", "windows-portable"],
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

    # build-windows-image command
    build_win_parser = subparsers.add_parser(
        "build-windows-image",
        help="Build the Windows base image for faster local testing",
    )
    build_win_parser.add_argument(
        "--rebuild", "-r",
        action="store_true",
        help="Force rebuild even if image exists",
    )
    build_win_parser.set_defaults(func=cmd_build_windows_image)

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

    # merge command
    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge results with existing gh-pages (for combining CI and local GPU runs)",
    )
    merge_parser.add_argument(
        "repo",
        help="GitHub repo in format 'owner/repo'",
    )
    merge_parser.add_argument(
        "--output-dir", "-o",
        default="comfy-test-results",
        help="Directory with test results (default: comfy-test-results)",
    )
    merge_parser.set_defaults(func=cmd_merge)

    # publish command
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish test results to gh-pages",
    )
    publish_parser.add_argument(
        "results_dir",
        help="Directory with test results (e.g., ~/logs/SAM3DBody-1445)",
    )
    publish_parser.add_argument(
        "--repo", "-r",
        required=True,
        help="GitHub repo in format 'owner/repo'",
    )
    publish_parser.add_argument(
        "--merge", "-m",
        action="store_true",
        help="Merge with existing gh-pages (use for CPU-only CI runs to preserve GPU results)",
    )
    publish_parser.set_defaults(func=cmd_publish)

    # generate-index command
    generate_index_parser = subparsers.add_parser(
        "generate-index",
        help="Generate index.html with platform tabs for a single branch directory",
    )
    generate_index_parser.add_argument(
        "output_dir",
        help="Directory containing platform subdirectories (e.g., gh-pages/main)",
    )
    generate_index_parser.add_argument(
        "--repo-name", "-r",
        help="Repository name for the header (e.g., owner/repo)",
    )
    generate_index_parser.set_defaults(func=cmd_generate_index)

    # generate-root-index command
    generate_root_index_parser = subparsers.add_parser(
        "generate-root-index",
        help="Generate root index.html with branch switcher for gh-pages",
    )
    generate_root_index_parser.add_argument(
        "output_dir",
        help="Root gh-pages directory containing branch subdirectories (main/, dev/, etc.)",
    )
    generate_root_index_parser.add_argument(
        "--repo-name", "-r",
        help="Repository name for the header (e.g., owner/repo)",
    )
    generate_root_index_parser.set_defaults(func=cmd_generate_root_index)

    # pool command
    pool_parser = subparsers.add_parser(
        "pool",
        help="Manage container pool for faster Linux testing",
    )
    pool_sub = pool_parser.add_subparsers(dest="pool_cmd")
    pool_start = pool_sub.add_parser("start", help="Start the container pool")
    pool_start.add_argument(
        "--size", "-n",
        type=int,
        default=2,
        help="Number of containers to keep ready (default: 2)",
    )
    pool_sub.add_parser("stop", help="Stop and destroy all pool containers")
    pool_sub.add_parser("status", help="Show pool status")
    pool_parser.set_defaults(func=cmd_pool)

    parsed_args = parser.parse_args(args)
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
