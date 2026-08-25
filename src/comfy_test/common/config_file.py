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
    TestConfig, TestLevel, WorkflowConfig, PlatformTestConfig, CoverageConfig,
    ALL_LEVELS, DEFAULT_LEVELS, resolve_python_version,
)
from .errors import ConfigError
from ..lanes.registry import resolve as _resolve_lane, allowed_tokens


# Config file names to search for
CONFIG_FILE_NAMES = [
    "comfy-test.toml",
]

# Folder names ComfyUI accepts for a pack's example workflows, copied verbatim
# from core so the two cannot drift:
#   ComfyUI/app/custom_node_manager.py -> example_workflow_folder_names
# Core globs custom_nodes/*/<folder>/*.json for /workflow_templates and serves
# each at /api/workflow_templates/<module>. Order is core's; the first entry is
# canonical -- core logs "consider renaming it to 'example_workflows'" for the
# other four, so we treat them as tolerated aliases, not equals.
EXAMPLE_WORKFLOW_DIRS = [
    "example_workflows",
    "example",
    "examples",
    "workflow",
    "workflows",
]
CANONICAL_WORKFLOW_DIR = EXAMPLE_WORKFLOW_DIRS[0]


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

        [test.lanes]
        # Explicit opt-in allowlist. Valid: linux, macos, windows,
        # windows_portable, macos_desktop, windows_desktop.
        lanes = ["linux", "macos", "windows", "windows_portable"]

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
    # str pins, list draws one at random per run, None = DEFAULT_PYTHON_VERSION
    python_version = test_section.get("python_version")
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
    # registry (comfy_test.lanes). Tokens are lane ids or aliases, e.g.:
    #   [test.lanes] lanes = ["linux-cpu", "windows-cuda", "macos-desktop"]
    # (bare "linux"/"macos"/"windows" are accepted as the cpu-gitcloned variant).
    # Only listed lanes run; an unknown token is an error.
    lanes = test_section.get("lanes", {})
    if not lanes and "platforms" in test_section:
        raise ConfigError(
            "[test.platforms] was renamed to [test.lanes]",
            'A lane is one (os x accelerator x install method) combination.\n\n'
            "    [test.lanes]\n"
            '    lanes = ["linux-cpu", "windows-cuda"]\n\n'
            "`platform` now means only what `sys.platform` and wheel tags mean.")
    _allow = lanes.get("lanes") if isinstance(lanes, dict) else None
    if lanes and not isinstance(_allow, list):
        raise ConfigError(
            "[test.lanes] must declare an explicit allowlist",
            'Use:  lanes = ["linux-cpu", "windows-cpu", "windows-cuda"]  '
            "(per-lane booleans like `linux = true` are no longer supported).")
    _allow = _allow or []
    _enabled_keys: set[str] = set()
    _bad = []
    for _tok in _allow:
        _p = _resolve_lane(str(_tok))
        if _p is None:
            _bad.append(_tok)
        else:
            _enabled_keys.add(_p.config_key)
    if _bad:
        raise ConfigError(
            f"Unknown lane(s) in [test.lanes] lanes: {_bad}",
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

    # Every platform config -- enabled iff its token is in the allowlist.
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
            "coverage": CoverageConfig(**test_section.get("coverage", {})),
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
            kwargs["python_version"] = resolve_python_version(python_version)
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
    except TypeError as e:
        # e.g. an unknown key under [test.coverage] (dataclass rejects it)
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

    Unknown keys are a hard error, mirroring the platform-token validator in
    load_config. They used to be silently ignored, which is how a node shipping
    `gpu = [...]` (there is no `gpu`; the accelerator is named by backend) got
    an empty cuda list, no warning, and a full 59-workflow run on a CUDA runner
    that was configured to run 3 (GeometryPack-2329, 2h of wrong work).
    """
    _KNOWN_KEYS = {
        "cpu", "cuda", "rocm", "timeout",
        # deprecated but still parsed:
        "run", "screenshot", "files", "file",
    }
    _unknown = sorted(set(data) - _KNOWN_KEYS)
    if _unknown:
        _hint = ""
        if "gpu" in _unknown:
            _hint = " ('gpu' is not a key -- the accelerator is named by backend: use 'cuda')"
        raise ConfigError(
            f"Unknown key(s) in [test.workflows]: {', '.join(_unknown)}{_hint}",
            "Valid keys: " + ", ".join(sorted(_KNOWN_KEYS)))

    # Consumer workflow folders, mirroring ComfyUI core's own list verbatim
    # (app/custom_node_manager.py: example_workflow_folder_names). Core globs
    # custom_nodes/*/<folder>/*.json to build /workflow_templates, and logs a
    # "consider renaming it to 'example_workflows'" nudge for the four aliases --
    # so example_workflows is canonical and the rest are tolerated. Discovering
    # only "workflows" made a pack following core's recommendation contribute
    # zero workflows, which the execution level then passed vacuously.
    consumer_dirs = [base_dir / name for name in EXAMPLE_WORKFLOW_DIRS]
    # comfy-test's own convention, not core's: core's glob is one level deep,
    # so a nested tests/ is never a template folder upstream.
    dev_tests_dirs = [d / "tests" for d in consumer_dirs]

    # Kept for _resolve_in_dirs' fallback and error messages: the canonical
    # location to point an author at when a named workflow is missing.
    workflows_dir = base_dir / CANONICAL_WORKFLOW_DIR

    def _glob_dirs(dirs: list) -> list:
        """Collect *.json across dirs, first match winning on duplicate names."""
        found, seen = [], set()
        for d in dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.json")):
                if f.name in seen:
                    continue
                seen.add(f.name)
                found.append(f)
        return found

    def _discover_all() -> list:
        """Discover every workflow JSON from all folder names ComfyUI recognises."""
        return sorted(_glob_dirs(consumer_dirs + dev_tests_dirs))

    def _discover_filtered() -> list:
        """Discover workflows filtered by COMFY_TEST_RUN_CONSUMER / COMFY_TEST_RUN_DEV settings."""
        from ..settings import _is_on, GENERAL_DEFAULTS
        run_consumer = _is_on("COMFY_TEST_RUN_CONSUMER", GENERAL_DEFAULTS["COMFY_TEST_RUN_CONSUMER"])
        run_dev = _is_on("COMFY_TEST_RUN_DEV", GENERAL_DEFAULTS["COMFY_TEST_RUN_DEV"])
        dirs = []
        if run_consumer:
            dirs.extend(consumer_dirs)
        if run_dev:
            dirs.extend(dev_tests_dirs)
        return sorted(_glob_dirs(dirs))

    def _resolve_in_dirs(filename: str) -> Path:
        """Resolve a filename against every folder name ComfyUI recognises,
        consumer folders first, then their tests/ subfolders."""
        name = filename if filename.endswith(".json") else filename + ".json"
        for d in (*consumer_dirs, *dev_tests_dirs):
            candidate = d / name
            if candidate.exists():
                return candidate
        # Fall back to workflows_dir (preserves old behaviour for missing files)
        return workflows_dir / name

    # Helper to resolve "all" or list of paths
    def _all_except(excludes, filtered):
        """Everything discovered, minus the named workflows."""
        all_wf = _discover_filtered() if filtered else _discover_all()
        exclude_names = {(f if f.endswith(".json") else f + ".json") for f in excludes}
        return [w for w in all_wf if w.name not in exclude_names]

    def resolve_workflows(value, filtered=False, key="workflows"):
        """Resolve one accelerator's selection. Exactly three forms:

            cuda = "all"                          everything discovered
            cpu  = ["basic", "upscale"]           exactly these
            cpu  = { exclude = ["heavy"] }        everything except these

        There is ONE way to exclude. The old per-item "!name" spelling is
        rejected: in a list it could silently switch the whole selection to
        "everything except", dropping any includes on the floor, so
        `["basic", "!heavy"]` read like an allowlist and ran every workflow.
        A table cannot express that mistake.
        """
        if value == "all":
            return _discover_filtered() if filtered else _discover_all()

        if isinstance(value, dict):
            unknown = sorted(set(value) - {"exclude"})
            if unknown:
                raise ConfigError(
                    f"Unknown key(s) in [test.workflows] {key}: {', '.join(unknown)}",
                    'The table form takes only `exclude`, e.g. '
                    f'{key} = {{ exclude = ["heavy"] }}. To list workflows '
                    f'explicitly use a plain array: {key} = ["basic", "upscale"].',
                )
            excludes = value.get("exclude") or []
            if not excludes:
                raise ConfigError(
                    f"[test.workflows] {key}.exclude is empty",
                    f'Name at least one workflow to exclude, or use {key} = "all".',
                )
            return _all_except(excludes, filtered)

        bangs = [f for f in value if f.startswith("!")]
        if bangs:
            keep = [f for f in value if not f.startswith("!")]
            drop = [b.lstrip("!") for b in bangs]
            raise ConfigError(
                f"[test.workflows] {key} uses the removed '!name' exclude syntax",
                f"Excluding is now spelled as a table, so it cannot be mixed with "
                f"includes:\n\n"
                f'    {key} = {{ exclude = {drop} }}\n\n'
                + (f"You also listed {keep}. A selection either names what to run "
                   f"or what to skip, never both -- pick one:\n\n"
                   f"    {key} = {keep}\n" if keep else ""),
            )
        return [_resolve_in_dirs(f) for f in value]

    # Auto-discover workflows (filtered by consumer/dev settings)
    workflows = resolve_workflows("all", filtered=True)

    # Parse accelerator workflow lists - supports "all" or list (with "!exclude").
    # Accelerator is named by backend: cpu / cuda / rocm. There is no "gpu".
    cpu = []
    cuda = []
    rocm = []
    if "cpu" in data:
        cpu = resolve_workflows(data["cpu"], filtered=True, key="cpu")
    if "cuda" in data:
        cuda = resolve_workflows(data["cuda"], filtered=True, key="cuda")
    if "rocm" in data:
        rocm = resolve_workflows(data["rocm"], filtered=True, key="rocm")

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
