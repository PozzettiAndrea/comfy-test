"""Load TestConfig from TOML files.

This module provides configuration loading for installation tests,
allowing custom nodes to define their test requirements in a TOML file.

Config file: comfy-test.toml

Example:
    [test]
    name = "ComfyUI-MyNode"
    python_version = "3.10"
    cpu_only = true
    levels = ["syntax", "install", "registration", "instantiation", "validation", "execution"]

    [test.workflows]
    timeout = 120

    # Workflows to run end-to-end (execution level)
    # Can be a list or "all" to auto-discover from workflows/ folder
    run = ["workflows/basic.json"]

    # Workflows to capture screenshots of
    # Can be a list or "all" to auto-discover from workflows/ folder
    screenshot = "all"
"""

import sys
from pathlib import Path
from typing import Optional, Dict, Any, List

# Use built-in tomllib (Python 3.11+) or tomli fallback
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore

from .config import TestConfig, TestLevel, WorkflowConfig, PlatformTestConfig
from ..errors import ConfigError


# Config file names to search for
CONFIG_FILE_NAMES = [
    "comfy-test.toml",
]


def load_config(
    path: Path | str,
    base_dir: Optional[Path] = None,
) -> TestConfig:
    """
    Load TestConfig from a TOML file.

    Args:
        path: Path to the TOML config file
        base_dir: Base directory for resolving relative paths (default: file's parent)

    Returns:
        Configured TestConfig instance

    Raises:
        FileNotFoundError: If config file doesn't exist
        ConfigError: If config is invalid
        ImportError: If tomli is not installed (Python < 3.11)

    Example:
        >>> config = load_config(Path("my_node/comfy-test.toml"))
        >>> print(config.name)
        'MyNode'
    """
    if tomllib is None:
        raise ImportError(
            "TOML parsing requires tomli for Python < 3.11. "
            "Install it with: pip install tomli"
        )

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    base_dir = Path(base_dir) if base_dir else path.parent

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ConfigError(f"Failed to parse TOML file: {path}", str(e))

    return _parse_config(data, base_dir)


def discover_config(
    node_dir: Optional[Path] = None,
    file_names: Optional[List[str]] = None,
) -> TestConfig:
    """
    Auto-discover and load config from a node directory.

    Searches for standard config file names in order of priority.

    Args:
        node_dir: Directory to search for config files (default: current directory)
        file_names: Custom list of file names to search (default: CONFIG_FILE_NAMES)

    Returns:
        TestConfig if config found

    Raises:
        ConfigError: If no config file is found

    Example:
        >>> config = discover_config(Path("my_custom_node/"))
        >>> print(f"Found config: {config.name}")
    """
    if tomllib is None:
        raise ImportError(
            "TOML parsing requires tomli for Python < 3.11. "
            "Install it with: pip install tomli"
        )

    node_dir = Path(node_dir) if node_dir else Path.cwd()
    file_names = file_names or CONFIG_FILE_NAMES

    for name in file_names:
        config_path = node_dir / name
        if config_path.exists():
            return load_config(config_path, node_dir)

    raise ConfigError(
        f"No config file found in {node_dir}",
        f"Searched for: {', '.join(file_names)}\n"
        "Create a comfy-test.toml file or specify --config path"
    )


def _parse_config(data: Dict[str, Any], base_dir: Path) -> TestConfig:
    """
    Parse TOML data into TestConfig.

    Expected format:
        [test]
        name = "MyNode"
        comfyui_version = "latest"
        python_version = "3.10"
        cpu_only = true
        timeout = 300
        levels = ["syntax", "install", "registration", "instantiation", "validation", "execution"]

        [test.platforms]
        linux = true
        windows = true
        windows_portable = true

        [test.workflows]
        timeout = 120
        run = ["workflows/basic.json"]
        screenshot = ["workflows/basic.json", "workflows/advanced.json"]

        # Legacy format (still supported):
        # files = ["workflows/basic.json"]  # maps to 'run'
        # file = "workflow.json"  # maps to 'run'

        [test.linux]
        skip_workflow = false

        [test.windows]
        skip_workflow = false

        [test.windows_portable]
        comfyui_portable_version = "latest"
        skip_workflow = false

    Args:
        data: Parsed TOML data
        base_dir: Base directory for resolving relative paths

    Returns:
        Configured TestConfig instance
    """
    test_section = data.get("test", {})

    if not test_section:
        raise ConfigError(
            "Missing [test] section in config file",
            "Your comfy-test.toml must have a [test] section with at least a name"
        )

    # Get basic test config
    name = test_section.get("name", base_dir.name)
    comfyui_version = test_section.get("comfyui_version", "latest")
    python_version = test_section.get("python_version", "3.10")
    cpu_only = test_section.get("cpu_only", True)
    timeout = test_section.get("timeout", 300)

    # Parse levels - default to all levels
    levels_raw = test_section.get("levels", ["syntax", "install", "registration", "instantiation", "validation", "execution"])
    levels = [TestLevel(l) for l in levels_raw]

    # Parse platforms section
    platforms = test_section.get("platforms", {})

    # Parse workflow section - support both new 'workflows' and legacy 'workflow'
    workflows_data = test_section.get("workflows", {})
    workflow_data = test_section.get("workflow", {})

    # Merge: new format takes precedence
    if workflows_data:
        workflow = _parse_workflow_config(workflows_data, base_dir)
    elif workflow_data:
        workflow = _parse_workflow_config(workflow_data, base_dir)
    else:
        workflow = _parse_workflow_config({}, base_dir)

    # Parse platform-specific configs
    linux_config = _parse_platform_config(
        test_section.get("linux", {}),
        platforms.get("linux", True)
    )
    windows_config = _parse_platform_config(
        test_section.get("windows", {}),
        platforms.get("windows", True)
    )
    windows_portable_config = _parse_platform_config(
        test_section.get("windows_portable", {}),
        platforms.get("windows_portable", True)
    )

    try:
        return TestConfig(
            name=name,
            comfyui_version=comfyui_version,
            python_version=python_version,
            cpu_only=cpu_only,
            timeout=timeout,
            levels=levels,
            workflow=workflow,
            linux=linux_config,
            windows=windows_config,
            windows_portable=windows_portable_config,
        )
    except ValueError as e:
        raise ConfigError("Invalid configuration", str(e))


def _parse_workflow_config(data: Dict[str, Any], base_dir: Path) -> WorkflowConfig:
    """Parse workflow configuration section.

    Supports:
      - New format: run = [...] or run = "all", screenshot = [...] or screenshot = "all"
      - Legacy format: files = [...] → maps to run
      - Legacy format: file = "..." → maps to run

    When "all" is specified, auto-discovers all *.json files in workflows/ directory.
    """
    run = []
    screenshot = []
    files = []

    # Helper to resolve "all" or list of paths
    def resolve_workflows(value):
        if value == "all":
            workflows_dir = base_dir / "workflows"
            if workflows_dir.exists():
                return sorted(workflows_dir.glob("*.json"))
            return []
        return [base_dir / f for f in value]

    # New format: run = [...] or "all", screenshot = [...] or "all"
    if "run" in data:
        run = resolve_workflows(data["run"])
    if "screenshot" in data:
        screenshot = resolve_workflows(data["screenshot"])

    # Legacy format: files = [...] → maps to run
    if "files" in data:
        files = [base_dir / f for f in data["files"]]

    # Legacy format: file = "..." → maps to run
    file_path = None
    if "file" in data:
        file_path = base_dir / data["file"]

    # Only pass timeout if explicitly set in config
    kwargs = {
        "run": run,
        "screenshot": screenshot,
        "files": files,
        "file": file_path,
    }
    if "timeout" in data:
        kwargs["timeout"] = data["timeout"]

    return WorkflowConfig(**kwargs)


def _parse_platform_config(data: Dict[str, Any], enabled: bool = True) -> PlatformTestConfig:
    """Parse platform-specific configuration."""
    return PlatformTestConfig(
        enabled=data.get("enabled", enabled),
        skip_workflow=data.get("skip_workflow", False),
        comfyui_portable_version=data.get("comfyui_portable_version"),
    )
