"""Run command for comfy-test CLI."""

import os
import shutil
import subprocess
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


def _detect_branch(node_dir: Path) -> str:
    """Best-effort git branch of the node repo, for the output namespace.

    The output tree is `{run}/{branch}/{platform}` and the branch level is
    never dropped (ADR-0016). When `--branch` is not given we default to the
    checked-out branch here: detached HEAD -> short SHA; not a git repo (or git
    missing) -> `local`. The result is flattened to a single path segment so a
    `feature/x` branch cannot add a level and break the shape.
    """
    def _git(*args) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(node_dir), *args],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    name = _git("rev-parse", "--abbrev-ref", "HEAD")
    if name == "HEAD":  # detached
        name = _git("rev-parse", "--short", "HEAD") or "detached"
    if not name:
        return "local"
    return name.replace("/", "-").replace("\\", "-").replace(" ", "-")


def get_current_platform() -> str:
    """Detect current OS and return matching platform name.

    COMFY_TEST_LANE overrides detection. Wrappers that choose the lane
    for the user (cds `--portable`) need it: auto-detection distinguishes
    venv-vs-portable by whether comfy-test's OWN interpreter is python_embeded,
    which is never true when a wrapper launches comfy-test from a normal
    python -- so the wrapper's choice used to be silently discarded
    (GeometryPack-1425: `--portable` produced an empty windows-portable-cuda
    log dir while the actual run executed platform `windows`). Invalid values
    raise rather than fall through: a platform request that cannot be honored
    must not silently become a different platform.
    """
    override = os.environ.get("COMFY_TEST_LANE", "").strip().lower().replace("-", "_")
    if override:
        valid = {"linux", "macos", "windows", "windows_portable"}
        if override not in valid:
            raise RuntimeError(
                f"COMFY_TEST_LANE={override!r} is not a lane "
                f"(valid: {', '.join(sorted(valid))})")
        if override.startswith("windows") and sys.platform != "win32":
            raise RuntimeError(
                f"COMFY_TEST_LANE={override!r} requires Windows "
                f"(running on {sys.platform})")
        if override == "macos" and sys.platform != "darwin":
            raise RuntimeError(
                f"COMFY_TEST_LANE={override!r} requires macOS "
                f"(running on {sys.platform})")
        if override == "linux" and sys.platform != "linux":
            raise RuntimeError(
                f"COMFY_TEST_LANE={override!r} requires Linux "
                f"(running on {sys.platform})")
        return override
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
    5. Install required node packs
    6. Run tests
    7. Output results to configured logs dir
    """
    from ..orchestration.manager import TestManager

    # Validate flag combos against host OS -- we never run cross-platform tests
    host = sys.platform
    if args.cuda and host == "darwin":
        print("[comfy-test] --cuda is not supported on macOS (no NVIDIA on Apple Silicon)",
              file=sys.stderr)
        return 1
    if args.portable and host != "win32":
        print("[comfy-test] --portable is only valid on Windows", file=sys.stderr)
        return 1

    # Desktop mode dispatches BEFORE any clone-to-tempdir: cdp_driver installs
    # the node via the Desktop app's Manager UI (registry tile), then swaps to
    # the target branch (--branch overrides; --dev shortcut sets branch=dev).
    # No local checkout needed -- cdp_driver fetches pyproject.toml /
    # comfy-test.toml / workflows/ via raw.githubusercontent + Manager clone.
    if getattr(args, "desktop", False) and not getattr(args, "nodelink", None):
        # Desktop mode installs from GitHub via the app's Manager, so the
        # runner must know the repo (the runners crash on nodelink=None).
        # When invoked bare, derive owner/repo from the cwd's origin remote.
        import re
        import subprocess as _sp
        try:
            _origin = _sp.run(["git", "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            _origin = ""
        if _origin.startswith("git@github.com:"):
            _origin = "https://github.com/" + _origin.split(":", 1)[1]
        _origin = re.sub(r"https://[^@/]+@", "https://", _origin)  # strip embedded creds
        _origin = _origin.removesuffix(".git")
        if "github.com" not in _origin:
            print("[comfy-test] --desktop installs from GitHub -- pass a repo "
                  "(comfy-test run <owner/repo> --desktop) or run from a clone "
                  "with a GitHub 'origin' remote.", file=sys.stderr)
            return 1
        print(f"[comfy-test] desktop: using repo from cwd origin: {_origin}")
        args.nodelink = _origin
    if getattr(args, "desktop", False):
        if host not in ("darwin", "win32"):
            print("[comfy-test] --desktop is only valid on macOS or Windows", file=sys.stderr)
            return 1
        if args.portable:
            print("[comfy-test] --desktop conflicts with --portable", file=sys.stderr)
            return 1
        from comfy_test.cli._desktop_runner import run_desktop
        if host == "darwin":
            mode = "mac"
        elif args.cuda:
            mode = "windows_cuda"
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

    # --branch is a CLONE argument. On a local checkout nothing is checked out,
    # but the value still becomes the branch segment of the output tree
    # (ADR-0016) -- so `--branch dev` on a checkout sitting on main files the
    # results under dev/, and publishing then overwrites the real dev results
    # with a run of different code. Refuse rather than silently mislabel.
    if getattr(args, "branch", None) and not _clone_tmpdir:
        detected = _detect_branch(node_dir)
        print(f"[comfy-test] --branch only applies when cloning; this is a local "
              f"checkout on '{detected}'.\n"
              f"[comfy-test] Results would be filed under '{args.branch}/' while "
              f"containing '{detected}' code. Drop --branch (it is detected "
              f"automatically), or point at a repo to clone.", file=sys.stderr)
        return 1

    # Cheap up-front gate. Without it, pointing at any directory builds a venv,
    # pins torch and clones ComfyUI before failing minutes later.
    problem = _nodelink.check_is_node_pack(node_dir)
    if problem:
        print(f"[comfy-test] {problem}", file=sys.stderr)
        return 1

    print(f"[comfy-test] Testing: {node_dir.name}")

    attach_mode = bool(getattr(args, "server_url", None))

    try:
        # Check if paths are configured. Attach mode needs no workspace (the
        # CI workflow prebuilt the env), so never run the interactive wizard
        # there -- stdin is not a TTY in CI and input() would crash.
        if not attach_mode and not are_paths_configured():
            run_setup_wizard()

        # Load config
        if args.config:
            config = load_config(args.config)
        else:
            config = discover_config()

        # Date + HH:MM (no seconds, by request). The date kills the cross-day
        # collision (a plain HH:MM reused a folder from a previous day); dropping
        # seconds keeps it readable, at the cost of a rare same-minute clash.
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        short_name = node_dir.name.removeprefix("ComfyUI-")

        if attach_mode:
            # No workspace: nothing is built. Scratch space only.
            work_dir = Path(tempfile.mkdtemp(prefix=f"comfy-test-attach-{short_name}-"))
        else:
            # Create workspace directory
            workspaces_dir = get_workspace_dir()
            workspaces_dir.mkdir(exist_ok=True)

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

        # Build output path: logs_dir/NodeName-XXXX/branch/platform-<backend>
        run_id = f"{short_name}-{timestamp}"
        # Branch level is never dropped (ADR-0016): --branch overrides, else we
        # detect the node repo's checked-out branch (fallback `local`).
        branch = getattr(args, 'branch', None) or _detect_branch(node_dir)
        cuda = args.cuda or os.environ.get("COMFY_TEST_CUDA") == "1"
        # `cuda` is the "accelerator active?" bool. It also names the on-disk
        # platform bucket (`<platform>-cuda` vs `<platform>-cpu`). ROCm is
        # reserved for later (a `COMFY_TEST_ROCM` sibling), not wired today.
        backend = "cuda" if cuda else "cpu"
        # Propagate args.cuda into COMFY_TEST_CUDA so the platform layer's
        # is_cuda_mode() (which only reads the env var) sees it. Without this,
        # `comfy-test run --cuda` silently runs ComfyUI in CPU mode.
        if cuda:
            os.environ["COMFY_TEST_CUDA"] = "1"
        # Propagate --torch-version (CLI > env var > config TOML > default).
        torch_version_override = getattr(args, "torch_version", None)
        if torch_version_override:
            config.torch_version = torch_version_override
        # Propagate --comfyui-version (CLI > config TOML > "latest" default).
        comfyui_version_override = getattr(args, "comfyui_version", None)
        if comfyui_version_override:
            config.comfyui_version = comfyui_version_override
        # External naming uses hyphens (gh-pages URLs, CI workflow inputs, artifact
        # names). The internal `platform` string is `windows_portable` for valid Python
        # identifier purposes; normalize the on-disk dir name to hyphens so e.g.
        # findstr/grep expressions in CI publish steps match without renaming.
        platform_dir = f"{platform.replace('_', '-')}-{backend}"
        # Always {run}/{branch}/{platform} -- consumers (publish, dashboard,
        # `cds show`) assume the three-level shape unconditionally (ADR-0016).
        output_dir = logs_dir / run_id / branch / platform_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        print(f"[comfy-test] Output: {output_dir}")
        print(f"[comfy-test] Platform: {platform}")

        # Create manager
        manager = TestManager(config, node_dir=node_dir, output_dir=output_dir)

        # Run tests
        level = TestLevel(args.level) if args.level else None
        workflow_filter = getattr(args, 'workflow', None)

        novram = getattr(args, 'novram', False)
        vram_debug = getattr(args, 'vram_debug', False)

        results = [manager.run_platform(
            platform,
            level,
            workflow_filter,
            work_dir=work_dir,
            novram=novram,
            vram_debug=vram_debug,
            server_url=getattr(args, "server_url", None),
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
        if args.cuda:
            flags.append("--cuda")
        if getattr(args, 'novram', False):
            flags.append("--novram")
        if getattr(args, 'vram_debug', False):
            flags.append("--vram-debug")
        if getattr(args, 'portable', False):
            flags.append("--portable")
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
        choices=[l.value for l in TestLevel],
        help="Run only up to this level (overrides config)",
    )
    run_parser.add_argument(
        "--cuda",
        action="store_true",
        help="Enable CUDA mode (uses real CUDA instead of mocking)",
    )
    run_parser.add_argument(
        "--server-url",
        help="Attach to an externally-managed ComfyUI server (CI boots it) "
             "instead of building an env + starting one. Run from the node's "
             "directory inside <ComfyUI>/custom_nodes/.",
    )
    run_parser.add_argument(
        "--portable",
        action="store_true",
        help="Use Windows Portable mode (only valid on Windows)",
    )
    run_parser.add_argument(
        "--desktop",
        action="store_true",
        help="macOS or Windows only: drive ComfyUI Desktop via CDP instead of "
             "running a server (--cuda on Windows means Electron + CUDA)",
    )
    run_parser.add_argument(
        "--dev",
        action="store_true",
        help="With --desktop: swap the installed node to the dev branch after "
             "Manager installs the CNR nightly. Shortcut for --branch dev; "
             "artifacts land under <run>/dev/{macos,windows}-desktop-dev/.",
    )
    run_parser.add_argument(
        "--refresh-app",
        action="store_true",
        help="With --desktop: discard the cached ComfyUI Desktop install and "
             "download/reinstall it. Normal runs reuse the cached app and "
             "only wipe ComfyUI user state.",
    )
    run_parser.add_argument(
        "--workflow", "-W",
        help="Run only this specific workflow",
    )
    run_parser.add_argument(
        "--branch", "-b",
        help="Branch to clone (remote nodelinks only). Also names the branch folder in the output path. On a local checkout the branch is detected automatically and passing this is an error.",
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
        "--torch-version",
        default=None,
        metavar="VERSION",
        help="Override torch_version in TestConfig. Accepts 'X.Y.Z' (auto-derives "
             "torchvision/torchaudio from common.config.TORCH_TRIPLES), 'latest' "
             "(opt out of pinning), or a slash-separated triple "
             "'torch/torchvision/torchaudio'. Default comes from TestConfig "
             "(comfy-test.toml) -> common.config.DEFAULT_TORCH_VERSION. "
             "Also reads $COMFY_TEST_TORCH_VERSION as an override.",
    )
    run_parser.add_argument(
        "--comfyui-version",
        default=None,
        metavar="VERSION",
        help="ComfyUI version to test against: 'latest' (the default), a git "
             "tag (e.g. v0.3.30), or a commit SHA. Overrides comfyui_version "
             "in comfy-test.toml [test].",
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
