"""Utilities for reading comfy-env.toml configuration."""

import sys
from pathlib import Path
from typing import List, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def get_node_reqs(node_dir: Path) -> List[Tuple[str, str]]:
    """
    Read comfy-env-root.toml and return list of node dependencies.

    Args:
        node_dir: Path to the custom node directory

    Returns:
        List of (name, repo) tuples, e.g., [('GeometryPack', 'PozzettiAndrea/ComfyUI-GeometryPack')]
    """
    config_path = Path(node_dir) / "comfy-env-root.toml"
    if not config_path.exists():
        return []

    try:
        config = tomllib.loads(config_path.read_text())
    except Exception:
        return []

    node_reqs = config.get("node_reqs", {})
    result = []

    for name, value in node_reqs.items():
        if isinstance(value, str):
            repo = value
        elif isinstance(value, dict):
            repo = value.get("repo", "")
        else:
            continue
        if repo:
            result.append((name, repo))

    return result


def get_env_vars(node_dir: Path) -> dict:
    """
    Read [env_vars] section from comfy-env.toml.

    Only used in CI environments where env vars can't persist between runs.

    Args:
        node_dir: Path to the custom node directory

    Returns:
        Dict of env var name -> value
    """
    import os

    # Only apply in CI environments
    if not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS"):
        return {}

    config_path = Path(node_dir) / "comfy-env.toml"
    if not config_path.exists():
        return {}

    try:
        config = tomllib.loads(config_path.read_text())
    except Exception:
        return {}

    env_vars = config.get("env_vars", {})
    return {str(k): str(v) for k, v in env_vars.items()}


def get_cuda_packages(node_dir: Path) -> List[str]:
    """
    Read all comfy-env.toml files and return list of CUDA package names.

    Searches recursively through the node directory for comfy-env.toml files
    and extracts CUDA packages from [cuda].packages sections.

    Args:
        node_dir: Path to the custom node directory

    Returns:
        List of CUDA package names (e.g., ['nvdiffrast', 'flash_attn', 'cumesh'])
    """
    cuda_packages = []

    # Search all comfy-env.toml files in the node tree (not just root)
    for config_path in Path(node_dir).rglob("comfy-env.toml"):
        try:
            config = tomllib.loads(config_path.read_text())
        except Exception:
            continue

        # Parse [cuda].packages = [...] structure
        cuda_section = config.get("cuda", {})
        if isinstance(cuda_section, dict):
            packages = cuda_section.get("packages", [])
            if isinstance(packages, list):
                for pkg in packages:
                    # Normalize: flash-attn -> flash_attn
                    cuda_packages.append(pkg.replace("-", "_"))

    return cuda_packages
