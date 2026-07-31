"""Load TestConfig from TOML files.

This module provides configuration loading for installation tests,
allowing custom nodes to define their test requirements in a TOML file.

Config file: comfy-test.toml

Example:
    [test]
    name = "ComfyUI-MyNode"
    levels = ["syntax", "install", "registration", "instantiation", "static_capture", "validation", "execution"]  # the default; or "all"

    [test.workflows]
    timeout = 120

    # Workflows to run end-to-end (execution level)
    run = ["basic.json"]

    # Workflows to screenshot:
    # - static_capture level: takes static screenshot (no execution)
    # - execution level: if also in 'run', captures with outputs visible
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

from .config import (
    TestConfig, TestLevel, WorkflowConfig, PlatformTestConfig,
    ALL_LEVELS, DEFAULT_LEVELS,
)
from .errors import ConfigError
from ..platforms.registry import resolve as _resolve_platform, allowed_tokens


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

    base_dir = Path(base_dir) if base_dir else path.resolve().parent

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
        comfyui_version = "latest"
        python_version = "3.10"
        timeout = 300
        levels = ["syntax", "install", "execution"]  # any subset, or "all"
        # Extra PyPI indexes (added as --extra-index-url, alongside PyTorch + pypi.org)
        extra_pip_indices = ["https://pypi.example.com/simple"]

        [test.platforms]
        # Explicit opt-in allowlist. Valid: linux, macos, windows,
        # windows_portable, macos_desktop, windows_desktop.
        platforms = ["linux", "macos", "windows", "windows_portable"]

        [test.workflows]
        timeout = 120
        cpu = "all"           # or ["!heavy"] to run all-except; cuda = [...] for CUDA
        run = ["basic.json"]  # (legacy) Resolved from workflows/ folder
        screenshot = ["basic.json", "advanced.json"]

        # Legacy format (still supported):
        # files = ["basic.json"]  # maps to 'run'
        # file = "basic.json"  # maps to 'run'

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
    name = base_dir.name  # Always use directory name
    comfyui_version = test_section.get("comfyui_version", "latest")
    python_version = test_section.get("python_version")  # None = random selection
    timeout = 600  # Fixed timeout for setup operations

    # Parse levels. Default + "all" derive from the single source of truth
    # (config.DEFAULT_LEVELS / ALL_LEVELS) so they can't drift; an explicit list
    # is taken as-is, accepting hyphens or underscores ("execution-light").
    levels_raw = test_section.get("levels")
    if levels_raw is None:
        levels = list(DEFAULT_LEVELS)
    elif levels_raw == "all":
        levels = list(ALL_LEVELS)
    else:
        levels = [TestLevel(l.replace("-", "_")) for l in levels_raw]

    # A configured custom hook ([test] custom = "...") auto-enables the CUSTOM
    # level (runs last), so setting the hook is enough -- no need to also list it.
    custom_hook = test_section.get("custom")
    if custom_hook and TestLevel.CUSTOM not in levels:
        levels.append(TestLevel.CUSTOM)

    # Platforms are an explicit opt-in allowlist, validated against the platform
    # registry (comfy_test.platforms). Tokens are platform ids or aliases, e.g.:
    #   [test.platforms] platforms = ["linux-cpu", "windows-cuda", "macos-desktop"]
    # (bare "linux"/"macos"/"windows" are accepted as the cpu-server variant).
    # Only listed platforms run; an unknown token is an error.
    platforms = test_section.get("platforms", {})
    _allow = platforms.get("platforms") if isinstance(platforms, dict) else None
    if platforms and not isinstance(_allow, list):
        raise ConfigError(
            "[test.platforms] must declare an explicit allowlist",
            'Use:  platforms = ["linux-cpu", "windows-cpu", "windows-cuda"]  '
            "(per-platform booleans like `linux = true` are no longer supported).")
    _allow = _allow or []
    _enabled_keys: set[str] = set()
    _bad = []
    for _tok in _allow:
        _p = _resolve_platform(str(_tok))
        if _p is None:
            _bad.append(_tok)
        else:
            _enabled_keys.add(_p.config_key)
    if _bad:
        raise ConfigError(
            f"Unknown platform(s) in [test.platforms] platforms: {_bad}",
            "Valid tokens: " + ", ".join(sorted(allowed_tokens())))

    def _os_enabled(config_key: str) -> bool:
        return config_key in _enabled_keys

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

    # Every platform config — enabled iff its token is in the allowlist.
    linux_config = _parse_platform_config(test_section.get("linux", {}), _os_enabled("linux"))
    macos_config = _parse_platform_config(test_section.get("macos", {}), _os_enabled("macos"))
    windows_config = _parse_platform_config(test_section.get("windows", {}), _os_enabled("windows"))
    windows_portable_config = _parse_platform_config(
        test_section.get("windows_portable", {}), _os_enabled("windows_portable"))
    macos_desktop_config = _parse_platform_config(
        test_section.get("macos_desktop", {}), _os_enabled("macos_desktop"))
    windows_desktop_config = _parse_platform_config(
        test_section.get("windows_desktop", {}), _os_enabled("windows_desktop"))
    linux_cuda_config = _parse_platform_config(
        test_section.get("linux_cuda", {}), _os_enabled("linux_cuda"))
    windows_cuda_config = _parse_platform_config(
        test_section.get("windows_cuda", {}), _os_enabled("windows_cuda"))
    windows_portable_cuda_config = _parse_platform_config(
        test_section.get("windows_portable_cuda", {}), _os_enabled("windows_portable_cuda"))
    windows_desktop_cuda_config = _parse_platform_config(
        test_section.get("windows_desktop_cuda", {}), _os_enabled("windows_desktop_cuda"))

    try:
        # Build kwargs, only include python_version if explicitly set
        kwargs = {
            "name": name,
            "comfyui_version": comfyui_version,
            "timeout": timeout,
            "levels": levels,
            "workflow": workflow,
            "linux": linux_config,
            "linux_cuda": linux_cuda_config,
            "macos": macos_config,
            "windows": windows_config,
            "windows_cuda": windows_cuda_config,
            "windows_portable": windows_portable_config,
            "windows_portable_cuda": windows_portable_cuda_config,
            "macos_desktop": macos_desktop_config,
            "windows_desktop": windows_desktop_config,
            "windows_desktop_cuda": windows_desktop_cuda_config,
        }
        if python_version is not None:
            kwargs["python_version"] = python_version
        if "res" in test_section:
            kwargs["res"] = test_section["res"]
        if "extra_pip_indices" in test_section:
            indices = test_section["extra_pip_indices"]
            if isinstance(indices, str):
                indices = [indices]
            if not isinstance(indices, list) or not all(isinstance(i, str) for i in indices):
                raise ConfigError(
                    "extra_pip_indices must be a list of index URL strings",
                    'e.g. extra_pip_indices = ["https://pypi.example.com/simple"]',
                )
            kwargs["extra_pip_indices"] = indices
        if custom_hook:
            kwargs["custom"] = custom_hook

        return TestConfig(**kwargs)
    except ValueError as e:
        raise ConfigError("Invalid configuration", str(e))


def _parse_workflow_config(data: Dict[str, Any], base_dir: Path) -> WorkflowConfig:
    """Parse workflow configuration section.

    All workflows in workflows/ and tests/ folders are auto-discovered and tested.
    Screenshots are always taken. Workflows run in alphabetical order.

    Supports (accelerator named by backend; there is no "gpu"):
      - cpu  = "all" or [...] - workflows to run on CPU runners
      - cuda = "all" or [...] - workflows to run on CUDA runners
      - rocm = "all" or [...] - reserved (no ROCm runner wired yet)
    Each list also supports "!name" entries meaning "all except these".
    """
    workflows_dir = base_dir / "workflows"
    dev_tests_dir = workflows_dir / "tests"

    def _discover_all() -> list:
        """Discover all workflow JSONs from workflows/ and workflows/tests/."""
        found = []
        for d in (workflows_dir, dev_tests_dir):
            if d.exists():
                found.extend(d.glob("*.json"))
        return sorted(found)

    def _discover_filtered() -> list:
        """Discover workflows filtered by COMFY_TEST_RUN_CONSUMER / COMFY_TEST_RUN_DEV settings."""
        from ..settings import _is_on, GENERAL_DEFAULTS
        run_consumer = _is_on("COMFY_TEST_RUN_CONSUMER", GENERAL_DEFAULTS["COMFY_TEST_RUN_CONSUMER"])
        run_dev = _is_on("COMFY_TEST_RUN_DEV", GENERAL_DEFAULTS["COMFY_TEST_RUN_DEV"])
        found = []
        if run_consumer and workflows_dir.exists():
            found.extend(workflows_dir.glob("*.json"))
        if run_dev and dev_tests_dir.exists():
            found.extend(dev_tests_dir.glob("*.json"))
        return sorted(found)

    def _resolve_in_dirs(filename: str) -> Path:
        """Resolve a filename against workflows/ then workflows/tests/."""
        name = filename if filename.endswith(".json") else filename + ".json"
        for d in (workflows_dir, dev_tests_dir):
            candidate = d / name
            if candidate.exists():
                return candidate
        # Fall back to workflows_dir (preserves old behaviour for missing files)
        return workflows_dir / name

    # Helper to resolve "all" or list of paths
    def resolve_workflows(value, filtered=False):
        if value == "all":
            return _discover_filtered() if filtered else _discover_all()
        # Exclusion mode: items starting with ! mean "all except these"
        excludes = [f.lstrip("!") for f in value if f.startswith("!")]
        if excludes:
            all_wf = _discover_filtered() if filtered else _discover_all()
            exclude_names = {(f if f.endswith(".json") else f + ".json") for f in excludes}
            return [w for w in all_wf if w.name not in exclude_names]
        return [_resolve_in_dirs(f) for f in value]

    # Auto-discover workflows (filtered by consumer/dev settings)
    workflows = resolve_workflows("all", filtered=True)

    # Parse accelerator workflow lists - supports "all" or list (with "!exclude").
    # Accelerator is named by backend: cpu / cuda / rocm. There is no "gpu".
    cpu = []
    cuda = []
    rocm = []
    if "cpu" in data:
        cpu = resolve_workflows(data["cpu"], filtered=True)
    if "cuda" in data:
        cuda = resolve_workflows(data["cuda"], filtered=True)
    if "rocm" in data:
        rocm = resolve_workflows(data["rocm"], filtered=True)

    # Legacy format support (backwards compatibility)
    run = []
    screenshot = []
    files = []
    file_path = None

    if "run" in data:
        run = resolve_workflows(data["run"])
        # If explicit run list provided, use it instead of auto-discover
        if run:
            workflows = run
        # Backward compat: if 'run' specified but no accelerator list, treat as cpu
        if not cpu and not cuda and not rocm:
            cpu = run
    if "screenshot" in data:
        screenshot = resolve_workflows(data["screenshot"])
    if "files" in data:
        files = [_resolve_in_dirs(f) for f in data["files"]]
        if files and not workflows:
            workflows = files
    if "file" in data:
        file_path = _resolve_in_dirs(data["file"])
        if file_path and not workflows:
            workflows = [file_path]

    # Build kwargs
    kwargs = {
        "workflows": workflows,
        "cpu": cpu,
        "cuda": cuda,
        "rocm": rocm,
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
