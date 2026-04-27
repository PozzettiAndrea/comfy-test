"""INSTALL level - Setup ComfyUI and install custom node."""

import sys
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

    Sets up ComfyUI and installs the custom node. This level handles two cases:
    1. comfyui_dir set: Use existing ComfyUI but install node fresh
    2. Neither: Full setup - clone ComfyUI and install node

    Args:
        ctx: Level context

    Returns:
        Updated context with platform, paths, cuda_packages, env_vars

    Raises:
        TestError: If setup fails
    """
    ctx.log(f"\n[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    platform = get_platform(ctx.platform_name, ctx.log)

    # Determine work directory
    if ctx.work_dir:
        work_path = ctx.work_dir
        work_path.mkdir(parents=True, exist_ok=True)
    else:
        # Create temporary directory - caller is responsible for cleanup
        work_path = Path(tempfile.mkdtemp(prefix="comfy_test_"))

    # Setup based on mode
    if ctx.comfyui_dir:
        paths = _setup_existing_with_install(ctx, platform, work_path)
    else:
        paths = _setup_full(ctx, platform, work_path)

    # Install validation endpoint (always needed for VALIDATION level)
    ctx.log("Installing validation endpoint...")
    platform.install_node_from_repo(
        paths,
        "PozzettiAndrea/ComfyUI-validate-endpoint",
        "ComfyUI-validate-endpoint"
    )

    # Get CUDA packages from comfy-env.toml. Whether we mock them depends on
    # whether the per-node pixi env actually has them installed — not on the
    # `--gpu` flag. comfy-env now inlines cuda-wheel URLs into pixi.toml when a
    # GPU is detected and a combo resolves, so on those runs the wheels live
    # in `<comfyui>/.ce/.pixi/envs/<env>/Lib/site-packages/<pkg>/`. On no-GPU
    # hosts the cuda-wheels resolution is skipped, the wheels aren't installed,
    # and we still need to mock them so `import flash_attn` doesn't crash node
    # code at import time.
    declared_cuda_packages = get_cuda_packages(ctx.node_dir)
    cuda_packages = [
        pkg for pkg in declared_cuda_packages
        if not _cuda_wheel_present(paths.comfyui_dir, pkg)
    ]
    if declared_cuda_packages:
        installed = [p for p in declared_cuda_packages if p not in cuda_packages]
        if installed:
            ctx.log(f"CUDA packages installed (no mock): {', '.join(installed)}")
        if cuda_packages:
            ctx.log(f"CUDA packages absent (will mock): {', '.join(cuda_packages)}")

    # Get env_vars from comfy-env.toml
    env_vars = get_env_vars(ctx.node_dir)
    if env_vars:
        ctx.log(f"Applying env_vars from comfy-env.toml: {', '.join(f'{k}={v}' for k, v in env_vars.items())}")

    # Install VRAM debug hooks if requested
    if ctx.vram_debug:
        _install_vram_debug(ctx, paths)

    return ctx.with_updates(
        platform=platform,
        paths=paths,
        cuda_packages=tuple(cuda_packages),
        env_vars=env_vars,
    )


def _setup_existing_with_install(
    ctx: LevelContext,
    platform: "TestPlatform",
    work_path: Path,
) -> TestPaths:
    """Use existing ComfyUI but install node."""
    ctx.log(f"Using existing ComfyUI: {ctx.comfyui_dir}")
    comfyui_path = Path(ctx.comfyui_dir).resolve()

    python_exe = _find_python(ctx.platform_name, comfyui_path)

    paths = TestPaths(
        work_dir=work_path,
        comfyui_dir=comfyui_path,
        python=python_exe,
        custom_nodes_dir=comfyui_path / "custom_nodes",
    )

    ctx.log("\nInstalling custom node...")
    platform.install_node(paths, ctx.node_dir, deps_installed=ctx.deps_installed)

    _install_node_dependencies(ctx, platform, paths)

    return paths


def _setup_full(
    ctx: LevelContext,
    platform: "TestPlatform",
    work_path: Path,
) -> TestPaths:
    """Full setup: clone ComfyUI and install node."""
    ctx.log("\nSetting up ComfyUI...")
    paths = platform.setup_comfyui(ctx.config, work_path)

    ctx.log("\nInstalling custom node...")
    platform.install_node(paths, ctx.node_dir, deps_installed=ctx.deps_installed)

    _install_node_dependencies(ctx, platform, paths)

    return paths


def _cuda_wheel_present(comfyui_dir: Path, pkg: str) -> bool:
    """True iff `pkg` is installed in any of the workspace's per-node pixi envs.

    Looks under `<comfyui_dir>/.ce/.pixi/envs/*/Lib/site-packages/` (Windows)
    and the equivalent `lib/python*/site-packages/` (Linux/macOS). Tolerates
    both `pkg/` (package dir) and `pkg.dist-info/` (metadata-only) layouts.
    Underscores and hyphens are normalized — e.g. `flash-attn` and `flash_attn`
    both match a `flash_attn/` site-packages dir.
    """
    if not comfyui_dir:
        return False
    envs_dir = Path(comfyui_dir) / ".ce" / ".pixi" / "envs"
    if not envs_dir.is_dir():
        return False

    norm = pkg.replace("-", "_").lower()
    candidate_names = {norm, pkg.replace("_", "-").lower()}

    for env_dir in envs_dir.iterdir():
        if not env_dir.is_dir():
            continue
        site_packages_candidates = [
            env_dir / "Lib" / "site-packages",                 # Windows pixi env
        ] + list((env_dir / "lib").glob("python*/site-packages"))  # POSIX pixi env
        for sp in site_packages_candidates:
            if not sp.is_dir():
                continue
            for entry in sp.iterdir():
                name = entry.name.lower().split("-")[0]
                if name in candidate_names:
                    return True
                if entry.name.lower().endswith(".dist-info"):
                    base = entry.name.rsplit("-", 1)[0].lower()
                    if base.replace("-", "_") in candidate_names:
                        return True
    return False


def _find_python(platform_name: str, comfyui_path: Path) -> Path:
    """Find Python executable for the platform."""
    if platform_name == "windows_portable":
        # For portable, find embedded Python
        python_embeded = comfyui_path.parent / "python_embeded"
        if not python_embeded.exists():
            python_embeded = comfyui_path.parent.parent / "python_embeded"
        return python_embeded / "python.exe"
    else:
        return Path(sys.executable)


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
