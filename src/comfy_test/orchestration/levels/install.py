"""INSTALL level - Setup ComfyUI and install custom node."""

import os
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
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
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

    # Get CUDA packages to mock from comfy-env.toml
    cuda_packages = get_cuda_packages(ctx.node_dir)
    gpu_mode = os.environ.get("COMFY_TEST_GPU")
    ctx.log(f"COMFY_TEST_GPU env var = {gpu_mode!r}")
    if gpu_mode:
        ctx.log("GPU mode: using real CUDA (no mocking)")
        cuda_packages = []
    elif cuda_packages:
        ctx.log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

    # Get env_vars from comfy-env.toml
    env_vars = get_env_vars(ctx.node_dir)
    if env_vars:
        ctx.log(f"Applying env_vars from comfy-env.toml: {', '.join(f'{k}={v}' for k, v in env_vars.items())}")

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

    ctx.log("Installing custom node...")
    platform.install_node(paths, ctx.node_dir, deps_installed=ctx.deps_installed)

    _install_node_dependencies(ctx, platform, paths)

    return paths


def _setup_full(
    ctx: LevelContext,
    platform: "TestPlatform",
    work_path: Path,
) -> TestPaths:
    """Full setup: clone ComfyUI and install node."""
    ctx.log("Setting up ComfyUI...")
    paths = platform.setup_comfyui(ctx.config, work_path)

    ctx.log("Installing custom node...")
    platform.install_node(paths, ctx.node_dir, deps_installed=ctx.deps_installed)

    _install_node_dependencies(ctx, platform, paths)

    return paths


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
