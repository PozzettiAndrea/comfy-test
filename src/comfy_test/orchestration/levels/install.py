"""INSTALL level - Setup ComfyUI and install custom node."""

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ...common.base_platform import TestPaths
from ...common.comfy_env import get_cuda_packages, get_env_vars, get_node_reqs
from ...common.errors import TestError
from ..context import LevelContext

if TYPE_CHECKING:
    from ...common.base_platform import TestPlatform


def get_platform(platform_name: str, log_callback=None) -> "TestPlatform":
    """Get platform instance by name."""
    if platform_name == "linux":
        from ...platforms.linux.platform import LinuxPlatform
        return LinuxPlatform(log_callback)
    elif platform_name == "windows":
        from ...platforms.windows.platform import WindowsPlatform
        return WindowsPlatform(log_callback)
    elif platform_name == "windows_portable":
        from ...platforms.windows_portable.platform import WindowsPortablePlatform
        return WindowsPortablePlatform(log_callback)
    elif platform_name == "macos":
        from ...platforms.macos.platform import MacOSPlatform
        return MacOSPlatform(log_callback)
    else:
        raise TestError(f"Unknown platform: {platform_name}")


def run(ctx: LevelContext) -> LevelContext:
    """Run INSTALL level.

    Clones ComfyUI, sets up the venv/pixi envs, and installs the custom node.

    Args:
        ctx: Level context

    Returns:
        Updated context with platform, paths, cuda_packages, env_vars

    Raises:
        TestError: If setup fails
    """
    ctx.log(f"\n[DEBUG] server={ctx.server}, api={ctx.api}")
    platform = get_platform(ctx.platform_name, ctx.log)

    if ctx.server_url:
        # Attach mode: CI prebuilt everything (venv, ComfyUI clone, node copy,
        # deps, validate-endpoint) and runs us from the node's directory inside
        # custom_nodes/. Derive paths from that layout instead of building.
        import sys
        node_dir = Path(ctx.node_dir).resolve()
        custom_nodes_dir = node_dir.parent
        comfyui_dir = custom_nodes_dir.parent
        paths = TestPaths(
            work_dir=comfyui_dir.parent,
            comfyui_dir=comfyui_dir,
            python=Path(sys.executable),
            custom_nodes_dir=custom_nodes_dir,
        )
        ctx.log(f"Attach mode: using prebuilt env at {comfyui_dir} "
                f"(server: {ctx.server_url})")
    else:
        # Determine work directory
        if ctx.work_dir:
            work_path = ctx.work_dir
            work_path.mkdir(parents=True, exist_ok=True)
        else:
            # Create temporary directory - caller is responsible for cleanup
            work_path = Path(tempfile.mkdtemp(prefix="comfy_test_"))

        paths = _setup_full(ctx, platform, work_path)

        # Install validation endpoint (always needed for VALIDATION level)
        ctx.log("Installing validation endpoint...")
        platform.install_node_from_repo(
            paths,
            "PozzettiAndrea/ComfyUI-validate-endpoint",
            "ComfyUI-validate-endpoint"
        )

    # Get CUDA packages from comfy-env.toml. Whether we mock them depends on
    # whether the per-node pixi env actually has them installed -- not on the
    # `--cuda` flag. comfy-env now inlines cuda-wheel URLs into pixi.toml when a
    # GPU is detected and a combo resolves, so on those runs the wheels live
    # in `<comfyui>/.ce/.pixi/envs/<env>/Lib/site-packages/<pkg>/`. On no-GPU
    # hosts the cuda-wheels resolution is skipped, the wheels aren't installed,
    # and we still need to mock them so `import flash_attn` doesn't crash node
    # code at import time.
    declared_cuda_packages = get_cuda_packages(ctx.node_dir)
    _py = getattr(paths, "python", None)
    cuda_packages = [
        pkg for pkg in declared_cuda_packages
        if not _cuda_wheel_present(paths.comfyui_dir, pkg, _py)
    ]
    if declared_cuda_packages:
        # Finding NO environment at all is a resolution failure, not evidence
        # of absence -- that is exactly how the stale `.ce` path silently mocked
        # everything for weeks. Say so, so the next layout change is a visible
        # warning instead of a wrong verdict.
        if not any(True for _ in _iter_env_site_packages(paths.comfyui_dir, _py)):
            _roots, _abi = _comfy_env_roots(paths.comfyui_dir, _py)
            ctx.log(
                "CUDA wheel check: no materialized comfy-env environment found "
                f"for abi={_abi or 'unknown'} (looked in {[str(r) for r in _roots]}); "
                "treating all declared CUDA packages as absent"
            )
        installed = [p for p in declared_cuda_packages if p not in cuda_packages]
        if installed:
            ctx.log(f"CUDA packages installed (no mock): {', '.join(installed)}")
            if _UNVERIFIED_ENV_DIRS:
                ctx.log(
                    "  note: matched in comfy-env dir(s) with no ABI tag, so the "
                    "build stack could not be verified -- re-run comfy-env install "
                    f"to re-materialize: {sorted(_UNVERIFIED_ENV_DIRS)}"
                )
        if cuda_packages:
            ctx.log(f"CUDA packages absent (will mock): {', '.join(cuda_packages)}")

    # Get env_vars from comfy-env.toml
    env_vars = get_env_vars(ctx.node_dir)
    if env_vars:
        ctx.log(f"Applying env_vars from comfy-env.toml: {', '.join(f'{k}={v}' for k, v in env_vars.items())}")

    # Install VRAM debug hooks if requested
    if ctx.vram_debug:
        _install_vram_debug(ctx, paths)

    # Provenance: ComfyUI version from the cloned/extracted tree's pyproject
    # (REGISTRATION refines this from the live server's /system_stats).
    comfyui_version = _read_comfyui_version(paths.comfyui_dir)
    comfyui_commit = _read_comfyui_commit(paths.comfyui_dir)
    if comfyui_version:
        ctx.log(f"ComfyUI under test: {comfyui_version}"
                + (f" ({comfyui_commit[:12]})" if comfyui_commit else ""))

    return ctx.with_updates(
        platform=platform,
        paths=paths,
        cuda_packages=tuple(cuda_packages),
        env_vars=env_vars,
        comfyui_version=comfyui_version,
        comfyui_commit=comfyui_commit,
    )


def _read_comfyui_version(comfyui_dir: Path) -> str | None:
    """Version from ComfyUI's pyproject.toml (works for git clones and the
    portable bundle, which ships the source tree without .git)."""
    try:
        import tomllib
        with open(comfyui_dir / "pyproject.toml", "rb") as f:
            return tomllib.load(f).get("project", {}).get("version") or None
    except Exception:
        return None


def _read_comfyui_commit(comfyui_dir: Path) -> str | None:
    """ComfyUI's checked-out commit SHA.

    The pyproject version only moves on releases, so many different HEADs
    share one version string -- and comfy-test clones HEAD by default
    (comfyui_version = "latest"). The SHA is the only field that identifies
    what was actually tested. None for the portable bundle (no .git)."""
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=comfyui_dir,
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def _setup_full(
    ctx: LevelContext,
    platform: "TestPlatform",
    work_path: Path,
) -> TestPaths:
    """Full setup: clone ComfyUI and install node."""
    ctx.log("\nSetting up ComfyUI...")
    paths = platform.setup_comfyui(ctx.config, work_path)

    ctx.log("\nInstalling custom node...")
    platform.install_node(paths, ctx.node_dir)

    _install_node_dependencies(ctx, platform, paths)

    return paths


_ENV_ROOTS_CACHE: dict = {}
_UNVERIFIED_ENV_DIRS: set = set()


def _comfy_env_roots(comfyui_dir, python_exe=None) -> list:
    """Directories that may contain materialized comfy-env environments.

    Ask comfy-env where its envs live rather than hardcoding a layout. This
    used to probe `<comfyui_dir>/.ce/.pixi/envs` only -- the v0.3.x layout.
    comfy-env moved to a machine-global root in 2026-07-31 (`4b21e5d`), so that
    path stopped existing and `_cuda_wheel_present` returned False for EVERY
    package on EVERY host, which made the INSTALL level log
    "CUDA packages absent (will mock)" unconditionally -- even with the wheels
    installed and a working GPU (measured: GeometryPack-1821 mocked cumesh and
    faithc_aot while both were present as `+cu128torch2.10` dist-infos).

    Resolution order: import comfy-env here; else ask the workspace venv, which
    always has it installed; else fall back to the legacy path so old
    workspaces still resolve.
    """
    key = (str(comfyui_dir), str(python_exe))
    if key in _ENV_ROOTS_CACHE:
        return _ENV_ROOTS_CACHE[key]

    roots = []
    abi = ""
    snippet = (
        "from comfy_env.environment.cache import get_workspace_dir, _abi_tag; "
        "print(get_workspace_dir(None)); print(_abi_tag())"
    )
    try:
        from comfy_env.environment.cache import (  # type: ignore
            get_workspace_dir, _abi_tag,
        )
        roots.append(Path(get_workspace_dir(None)) / "envs")
        abi = _abi_tag()
    except Exception:
        if python_exe:
            try:
                out = subprocess.run(
                    [str(python_exe), "-c", snippet],
                    capture_output=True, text=True, timeout=60,
                )
                # comfy-env prints its banner to stderr; stdout is root then tag.
                lines = [l.strip() for l in (out.stdout or "").strip().splitlines() if l.strip()]
                if lines:
                    roots.append(Path(lines[0]) / "envs")
                if len(lines) > 1:
                    abi = lines[1]
            except Exception:
                pass
    if comfyui_dir:
        roots.append(Path(comfyui_dir) / ".ce" / ".pixi" / "envs")  # legacy v0.3.x

    _ENV_ROOTS_CACHE[key] = (roots, abi)
    return _ENV_ROOTS_CACHE[key]


def _iter_env_site_packages(comfyui_dir, python_exe=None):
    """Yield site-packages dirs of envs built for THIS stack.

    comfy-env tags env directories with the ABI they were built against
    (`<name>-py313-torch2-10-cu128`). Scanning every directory would let a wheel
    from another stack count as "installed" -- this box really does have
    `cumesh-0.0.1+cu128torch2.8` and `cumesh-0.0.1+cu128torch2.10` for the same
    node -- which is the same false verdict this function exists to stop, just
    from a different direction. Accept only an exact tag match, or an untagged
    directory left over from before comfy-env started tagging.
    """
    roots, abi = _comfy_env_roots(comfyui_dir, python_exe)
    for root in roots:
        if not root.is_dir():
            continue
        for env_dir in root.iterdir():
            if not env_dir.is_dir():
                continue
            tagged = "-py" in env_dir.name
            if abi and tagged and not env_dir.name.endswith("-" + abi):
                continue  # tagged for a different python/torch/backend
            if not tagged:
                # Pre-dates ABI tagging: we cannot tell what it was built
                # against. Still scanned, so a box that has not re-materialized
                # yet does not regress to mocking everything -- but record it so
                # the verdict is not presented as verified.
                _UNVERIFIED_ENV_DIRS.add(str(env_dir))
            # v0.4: <root>/envs/<name-abi>/.pixi/envs/default/
            # v0.3: <comfyui>/.ce/.pixi/envs/<name>/
            for base in (env_dir / ".pixi" / "envs" / "default", env_dir):
                if not base.is_dir():
                    continue
                candidates = [base / "Lib" / "site-packages"]
                candidates += list((base / "lib").glob("python*/site-packages"))
                for sp in candidates:
                    if sp.is_dir():
                        yield sp


def _cuda_wheel_present(comfyui_dir: Path, pkg: str, python_exe=None) -> bool:
    """True iff `pkg` is installed in any materialized comfy-env environment.

    Tolerates both `pkg/` (package dir) and `pkg.dist-info/` (metadata-only)
    layouts. Underscores and hyphens are normalized -- e.g. `flash-attn` and
    `flash_attn` both match a `flash_attn/` site-packages dir.
    """
    norm = pkg.replace("-", "_").lower()
    candidate_names = {norm, pkg.replace("_", "-").lower()}

    for sp in _iter_env_site_packages(comfyui_dir, python_exe):
        for entry in sp.iterdir():
            name = entry.name.lower().split("-")[0]
            if name in candidate_names:
                return True
            if entry.name.lower().endswith(".dist-info"):
                base = entry.name.rsplit("-", 1)[0].lower()
                if base.replace("-", "_") in candidate_names:
                    return True
    return False


def _install_vram_debug(ctx: LevelContext, paths: TestPaths) -> None:
    """Drop a .pth file into the test venv for VRAM debug hooks.

    .pth files ARE processed from venv site-packages (unlike sitecustomize.py
    which is only loaded from the system site-packages).
    """
    from ...debug.vram import get_pth_content

    # Find site-packages: <venv>/lib/pythonX.Y/site-packages/
    venv_dir = paths.python.parent.parent
    lib_dir = venv_dir / "lib"
    if not lib_dir.exists():
        ctx.log("[VRAM] Warning: could not find venv lib dir, skipping .pth install")
        return

    # Find the pythonX.Y directory
    python_dirs = [d for d in lib_dir.iterdir() if d.name.startswith("python") and d.is_dir()]
    if not python_dirs:
        ctx.log("[VRAM] Warning: could not find python dir in venv, skipping .pth install")
        return

    site_packages = python_dirs[0] / "site-packages"
    if not site_packages.exists():
        ctx.log("[VRAM] Warning: site-packages not found, skipping .pth install")
        return

    target = site_packages / "_comfy_test_vram_debug.pth"
    target.write_text(get_pth_content())
    ctx.log(f"[VRAM] Installed .pth file -> {target}")


def _install_node_dependencies(
    ctx: LevelContext,
    platform: "TestPlatform",
    paths: TestPaths,
) -> None:
    """Install node dependencies from comfy-env.toml."""
    node_reqs = get_node_reqs(ctx.node_dir)
    if node_reqs:
        ctx.log(f"Installing {len(node_reqs)} node dependency(ies)...")
        for name, repo in node_reqs:
            ctx.log(f"  {name} from {repo}")
            platform.install_node_from_repo(paths, repo, name)
