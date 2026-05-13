"""Run command for comfy-test CLI."""

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from ..common.config import TestLevel
from ..common.config_file import discover_config, load_config
from ..common.errors import TestError, ConfigError
from . import _nodelink
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

    1. Resolve <nodelink> positional (URL -> clone, local path -> cd, empty -> cwd)
    2. Create workspace in configured workspace dir
    3. Clone ComfyUI and create venv
    4. Copy node into custom_nodes/
    5. Install node dependencies
    6. Run tests
    7. Output results to configured logs dir
    """
    from ..orchestration.manager import TestManager

    # Validate flag combos against host OS -- we never run cross-platform tests
    host = sys.platform
    if args.gpu and host == "darwin":
        print("[comfy-test] --gpu is not supported on macOS (no NVIDIA on Apple Silicon)",
              file=sys.stderr)
        return 1
    if args.portable and host != "win32":
        print("[comfy-test] --portable is only valid on Windows", file=sys.stderr)
        return 1

    # Desktop mode dispatches BEFORE any clone-to-tempdir: ComfyUI Desktop's
    # Manager only installs from main of the GitHub URL, so a local checkout
    # or non-main branch is meaningless. cdp_driver fetches pyproject.toml
    # / comfy-test.toml / workflows/ via raw.githubusercontent direct from
    # main -- no local source needed.
    if getattr(args, "desktop", False):
        if host not in ("darwin", "win32"):
            print("[comfy-test] --desktop is only valid on macOS or Windows", file=sys.stderr)
            return 1
        if args.portable:
            print("[comfy-test] --desktop conflicts with --portable", file=sys.stderr)
            return 1
        if args.branch and args.branch != "main":
            print(f"[comfy-test] --desktop installs from main regardless of "
                  f"--branch {args.branch!r}; Manager-driven flow has no "
                  f"branch selection. Results will land under "
                  f"gh-pages/{args.branch}/<platform>/ to match the user's "
                  f"intent.", file=sys.stderr)
        # NOTE: don't overwrite args.branch -- _desktop_runner uses it for the
        # logs path layout (gh-pages/<branch>/<platform>/), while NODE_BRANCH
        # is hardcoded to "main" separately when invoking cdp_driver.
        from comfy_test.cli._desktop_runner import run_desktop
        if host == "darwin":
            mode = "mac"
        elif args.gpu:
            mode = "windows_gpu"
        else:
            mode = "windows"
        return run_desktop(args, mode)

    # Resolve <nodelink> positional. Three modes (non-desktop only):
    #   empty            -> cwd is the node dir (legacy behavior)
    #   local path       -> chdir into it
    #   URL / owner/repo -> shallow-clone to a temp dir, chdir into it
    _clone_tmpdir = None
    nodelink = getattr(args, "nodelink", None)
    if nodelink:
        if _nodelink.is_url_nodelink(nodelink):
            _clone_tmpdir = Path(tempfile.mkdtemp(prefix="comfy-test-run-"))
            try:
                name = _nodelink.clone_node(nodelink, args.branch, _clone_tmpdir,
                                            log_prefix="[comfy-test]")
            except Exception as e:
                print(f"[comfy-test] {e}", file=sys.stderr)
                shutil.rmtree(_clone_tmpdir, ignore_errors=True)
                return 1
            os.chdir(_clone_tmpdir / name)
        else:
            local = Path(_nodelink.expand_nodelink(nodelink)).resolve()
            if not local.is_dir():
                print(f"[comfy-test] Local path is not a directory: {local}", file=sys.stderr)
                return 1
            os.chdir(local)

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

        # Platform is always derived from the host OS -- we never run cross-platform.
        platform = get_current_platform()
        if args.portable:
            platform = "windows_portable"

        # Build output path: logs_dir/NodeName-XXXX/branch/platform-gpu
        run_id = f"{short_name}-{timestamp}"
        branch = getattr(args, 'branch', None)
        gpu = args.gpu or os.environ.get("COMFY_TEST_GPU") == "1"
        gpu_suffix = "gpu" if gpu else "cpu"
        # External naming uses hyphens (gh-pages URLs, CI workflow inputs, artifact
        # names). The internal `platform` string is `windows_portable` for valid Python
        # identifier purposes; normalize the on-disk dir name to hyphens so e.g.
        # findstr/grep expressions in CI publish steps match without renaming.
        platform_dir = f"{platform.replace('_', '-')}-{gpu_suffix}"
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
        vram_debug = getattr(args, 'vram_debug', False)

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
            vram_debug=vram_debug,
        )]

        # Copy per-platform server.log into output_dir so it ships with the
        # artifact upload (work_dir lives under COMFY_TEST_WORKSPACE_DIR which
        # is NOT part of the CI artifact path; only COMFY_TEST_LOGS_DIR is).
        server_log_src = work_dir / "server.log"
        if server_log_src.exists():
            try:
                shutil.copy2(server_log_src, output_dir / "server.log")
            except OSError:
                pass

        # Report results
        flags = []
        if args.gpu:
            flags.append("--gpu")
        if getattr(args, 'novram', False):
            flags.append("--novram")
        if getattr(args, 'vram_debug', False):
            flags.append("--vram-debug")
        if getattr(args, 'portable', False):
            flags.append("--portable")
        if getattr(args, 'server_url', None):
            flags.append("--server-url")
        if getattr(args, 'comfyui_dir', None):
            flags.append("--comfyui-dir")
        if level:
            flags.append(f"--level={level}")
        if workflow_filter:
            flags.append(f"--workflow={workflow_filter}")
        flag_suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"\n{'='*60}")
        print(f"RESULTS{flag_suffix}")
        print(f"{'='*60}")

        all_passed = True
        for result in results:
            status = "PASS" if result.success else "FAIL"
            print(f"  {result.platform}: {status}")
            if not result.success:
                all_passed = False
                if result.error:
                    print(f"    Error: {_safe_str(result.error)}")

        # Per-workflow resource summary
        results_file = output_dir / "results.json"
        if results_file.exists():
            import json as _json
            results_data = _json.loads(results_file.read_text())
            workflows = [w for w in results_data.get("workflows", []) if w.get("resources")]
            if workflows:
                has_vram = any(w.get("resources", {}).get("vram") for w in workflows)
                header = f"\n  {'Workflow':<30s} {'Status':<9s} {'Time':<10s}"
                header += " Peak VRAM  " if has_vram else ""
                header += " Peak RAM"
                print(header)
                total_duration = 0.0
                for w in workflows:
                    name = w["name"] + ".json"
                    st = w["status"].upper()
                    res = w.get("resources", {})
                    dur = w.get("duration_seconds", 0)
                    total_duration += dur
                    mins, secs = divmod(int(dur), 60)
                    line = f"  {name:<30s} {st:<9s} {mins:02d}:{secs:02d}     "
                    if has_vram:
                        vram = res.get("vram", {}).get("peak")
                        line += f" {vram:>5.2f} GB   " if vram is not None else "     -      "
                    ram = res.get("ram", {}).get("peak")
                    line += f" {ram:>5.2f} GB" if ram is not None else "    -"
                    print(line)
                total_mins, total_secs = divmod(int(total_duration), 60)
                print(f"\n  Total execution time: {total_mins:02d}:{total_secs:02d}")

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
    finally:
        # Clean up the temp clone dir if we made one for a URL nodelink.
        if _clone_tmpdir is not None:
            shutil.rmtree(_clone_tmpdir, ignore_errors=True)


def add_run_parser(subparsers):
    """Add the run subcommand parser."""
    run_parser = subparsers.add_parser(
        "run",
        help="Run tests (native; takes a URL, local path, or nothing for cwd)",
    )
    run_parser.add_argument(
        "nodelink",
        nargs="?",
        default=None,
        help="Git URL, owner/repo shorthand, or local path. Omit to use current directory.",
    )
    run_parser.add_argument(
        "--config", "-c",
        help="Path to config file (default: auto-discover)",
    )
    run_parser.add_argument(
        "--level", "-l",
        choices=["syntax", "install", "registration", "instantiation", "static_capture", "validation", "execution_light", "execution"],
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
        "--desktop",
        action="store_true",
        help="Drive ComfyUI Desktop via CDP instead of running a server "
             "(macOS or Windows; --gpu on Windows means Electron + CUDA)",
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
    run_parser.add_argument(
        "--vram-debug",
        action="store_true",
        help="Enable VRAM debug logging (logs model load/unload with per-module breakdown)",
    )
    run_parser.add_argument(
        "--monitor-progress", type=int, default=None, metavar="PORT",
        help="--desktop only: serve a live viewer on http://localhost:<PORT>/ "
             "with the latest cdp_driver frame + session.log + comfyui.log tails. "
             "Useful while iterating on the desktop driver.",
    )
    run_parser.set_defaults(func=cmd_run)
