"""Configuration dataclasses for installation tests."""

import os
import random
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, List, Tuple

# Python versions comfy-test knows how to build a venv for. This is the
# *validation* set -- what may be asked for -- not what is picked by default.
PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13"]

# What a run uses when the config says nothing. A fixed default rather than a
# draw over PYTHON_VERSIONS: an unpinned random interpreter meant a re-run
# could go green with no fix, which is the single most confusing behaviour the
# tool had. Widen deliberately by asking for a list.
DEFAULT_PYTHON_VERSION = "3.13"

# Pinned torch family. Default tracks the most recent fully-aligned set on
# PyPI -- torch, torchvision, torchaudio released in lockstep with matching
# CUDA-bundle versions. We pin this explicitly because torch's release
# cadence has pulled ahead of torchaudio's (e.g. torch 2.12.0 shipped
# 2026-05-13 with CUDA 13 but torchaudio is still at 2.11.0 with CUDA 12.8,
# producing skewed venvs when uv resolves freely). Bump this constant
# whenever a new fully-aligned set is published.
#
# To opt out of pinning (use whatever uv resolves), set torch_version to
# "latest" via CLI flag, env var, or comfy-test.toml. To override the auto-
# derived auxiliary versions, pass a slash-separated triple like
# "2.13.0/0.28.0/2.13.0".
DEFAULT_TORCH_VERSION = "2.10.0"

# Known-good torch / torchvision / torchaudio triples available on PyPI as
# cp310/cp311/cp312/cp313 manylinux + win + macos wheels. Verify wheel
# availability on pypi.org (or pytorch.org/whl/cu128) before adding entries.
TORCH_TRIPLES: dict = {
    "2.11.0": ("0.26.0", "2.11.0"),
    "2.10.0": ("0.25.0", "2.10.0"),
    "2.9.1":  ("0.24.1", "2.9.1"),
    "2.9.0":  ("0.24.0", "2.9.0"),
    "2.8.0":  ("0.23.0", "2.8.0"),
}


def resolve_torch_triple(version: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """Resolve a torch_version specifier to a (torch, torchvision, torchaudio) triple.

    Accepts:
      None or ""    -> use DEFAULT_TORCH_VERSION
      "latest"      -> None (opt out of pinning; let uv resolve freely)
      "X.Y.Z"       -> auto-derive from TORCH_TRIPLES; raises if unknown
      "T/V/A"       -> slash-separated explicit triple (escape hatch for
                       versions not in TORCH_TRIPLES yet)
    """
    if version is None or version == "":
        version = DEFAULT_TORCH_VERSION
    if version == "latest":
        return None
    if "/" in version:
        parts = version.split("/")
        if len(parts) != 3:
            raise ValueError(
                f"torch_version slash-form must have 3 parts (torch/torchvision/torchaudio), got {version!r}"
            )
        return (parts[0], parts[1], parts[2])
    if version not in TORCH_TRIPLES:
        raise ValueError(
            f"torch_version {version!r} not in TORCH_TRIPLES. Known: {sorted(TORCH_TRIPLES)}. "
            f"Pass a slash-separated triple ('2.13.0/0.28.0/2.13.0') to override."
        )
    tv, ta = TORCH_TRIPLES[version]
    return (version, tv, ta)


def resolve_python_version(requested=None) -> str:
    """Resolve the interpreter a run should use.

    Precedence, highest first:

    1. ``$COMFY_TEST_PYTHON_VERSION`` -- an explicit pin. CI sets this so the
       chosen version is visible at the run level, and a deliberate override
       wins even over a config list.
    2. ``requested`` from ``[test] python_version``: a single version pins it,
       a list draws one at random per run.
    3. ``DEFAULT_PYTHON_VERSION``.

    Anything outside PYTHON_VERSIONS raises rather than silently falling back,
    so a typo is a hard error instead of a run against the wrong interpreter.
    """
    import os
    pinned = os.environ.get("COMFY_TEST_PYTHON_VERSION", "").strip()
    if pinned:
        if pinned not in PYTHON_VERSIONS:
            raise ValueError(
                f"COMFY_TEST_PYTHON_VERSION={pinned!r} is not a supported Python "
                f"version. Known: {', '.join(PYTHON_VERSIONS)}"
            )
        return pinned

    if requested is None:
        return DEFAULT_PYTHON_VERSION

    if isinstance(requested, str):
        candidates = [requested]
    else:
        candidates = [str(v).strip() for v in requested if str(v).strip()]
        if not candidates:
            raise ValueError(
                "[test] python_version is an empty list -- give at least one "
                f"version, or omit the key for the default ({DEFAULT_PYTHON_VERSION})."
            )

    unknown = [v for v in candidates if v not in PYTHON_VERSIONS]
    if unknown:
        raise ValueError(
            f"Unsupported Python version(s) in [test] python_version: "
            f"{', '.join(unknown)}. Known: {', '.join(PYTHON_VERSIONS)}"
        )
    return random.choice(candidates)


def _default_python_version() -> str:
    """Dataclass default: env pin, else DEFAULT_PYTHON_VERSION."""
    return resolve_python_version(None)


class TestLevel(str, Enum):
    """Test levels - each is explicit, run what's in the list.

    - syntax: Check project structure (pyproject.toml vs requirements.txt)
    - coverage: Check every registered node is used by a workflow (static, no install)
    - warnings: Opt-in antipattern report on pack layout (static, no install, never fails)
    - hazards: Opt-in report on how the pack behaves inside ComfyUI's process
      (static, no install, never fails). Findings are grouped by confidence band.
    - install: Setup ComfyUI, install node, install deps
    - registration: Start server, check nodes in object_info (requires install)
    - instantiation: Call each node's constructor (requires install)
    - static_capture: Take static screenshots of workflows (requires install)
    - validation: Validate workflows via /validate endpoint (requires install)
    - execution: Run workflows end-to-end, capture with outputs (requires install, may require GPU)

    Dependencies:
    - syntax: standalone
    - coverage: standalone
    - install: standalone
    - registration, instantiation, static_capture, validation, execution: all require install
    """
    SYNTAX = "syntax"
    COVERAGE = "coverage"
    WARNINGS = "warnings"  # opt-in antipattern report; report-only, never fails
    HAZARDS = "hazards"    # opt-in runtime-behaviour report; report-only, never fails
    INSTALL = "install"
    REGISTRATION = "registration"
    JAVASCRIPT = "javascript"  # after registration: the pack's web/ is generated at server boot
    INSTANTIATION = "instantiation"
    STATIC_CAPTURE = "static_capture"
    VALIDATION = "validation"
    EXECUTION_LIGHT = "execution_light"
    EXECUTION = "execution"
    CUSTOM = "custom"  # node-supplied hook ([test] custom = "..."); runs last

    @classmethod
    def requires(cls, level: "TestLevel") -> List[str]:
        """Resources this level consumes (see LEVEL_REQUIRES / the resource model)."""
        return LEVEL_REQUIRES.get(level, [])

    @classmethod
    def get_dependencies(cls, level: "TestLevel") -> List["TestLevel"]:
        """Direct prerequisite levels, derived from the resources this level
        requires (each resource is built by a level). Callers wanting the full,
        transitive set should use resolve_dependencies().
        """
        return [RESOURCE_PROVIDERS[r] for r in cls.requires(level)
                if RESOURCE_PROVIDERS.get(r) is not None]

    @classmethod
    def resolve_dependencies(cls, levels: List["TestLevel"]) -> List["TestLevel"]:
        """Given the checks the user asked for, pull in whatever PROVIDES the
        resources they need -- transitively -- and return them in execution order.

        A check declares `requires = [resources]`; it never names a level. So the
        engine (not the check author) decides that e.g. needing `api` means the
        server -- and therefore the env -- must be built first. This is what lets
        a user pick a check without knowing its prerequisites.
        """
        needed = set(levels)
        frontier = list(levels)
        while frontier:
            lvl = frontier.pop()
            for res in cls.requires(lvl):
                provider = RESOURCE_PROVIDERS.get(res)
                if provider is not None and provider not in needed:
                    needed.add(provider)
                    frontier.append(provider)  # resolve the provider's needs too
        # Return in execution order (the enum is declared in that order).
        return [l for l in cls if l in needed]


# --- Resource model: checks require resources, not levels --------------------
# A "resource" is a live capability with a lifecycle, threaded through the
# LevelContext. A check declares which resources it needs; the engine ensures
# whatever PROVIDES each resource has run first (see resolve_dependencies).
#
#   env    -- built venv + cloned ComfyUI + installed node   (ctx.paths / platform)
#   server -- a running ComfyUI process                       (ctx.server)
#   api    -- an HTTP client bound to that server             (ctx.api)
#
# The server is SESSION-scoped: built once by its provider and reused by every
# check that needs it. We deliberately do not start it twice (a per-check "fresh
# server" scope is a future knob, added only when a check actually needs it).
#
# RESOURCE_PROVIDERS: which level builds each resource.
RESOURCE_PROVIDERS: Dict[str, TestLevel] = {
    "env": TestLevel.INSTALL,          # INSTALL builds the env
    "server": TestLevel.REGISTRATION,  # REGISTRATION boots the server ...
    "api": TestLevel.REGISTRATION,     # ... and its API client comes up with it
}

# LEVEL_REQUIRES: which resources each level consumes. This is the ONLY place a
# level's prerequisites are declared -- add a new check by listing what it needs.
LEVEL_REQUIRES: Dict[TestLevel, List[str]] = {
    TestLevel.SYNTAX: [],                     # static source check, needs nothing
    TestLevel.COVERAGE: [],                   # static workflow/registry check
    TestLevel.WARNINGS: [],                   # static source check, needs nothing
    TestLevel.HAZARDS: [],                    # static source check, needs nothing
    TestLevel.INSTALL: [],                    # provider of `env`
    TestLevel.REGISTRATION: ["env"],          # provider of `server` + `api`
    TestLevel.JAVASCRIPT: ["server"],         # reads web/ generated at server boot
    TestLevel.INSTANTIATION: ["env"],         # spawns its own subprocess from the env
    TestLevel.STATIC_CAPTURE: ["server"],     # browses the live server
    TestLevel.VALIDATION: ["api"],            # POSTs /validate
    TestLevel.EXECUTION_LIGHT: ["server"],
    TestLevel.EXECUTION: ["server"],
    TestLevel.CUSTOM: ["server", "api"],      # node hook gets the live server + api
}


# Canonical level sets -- the single source of truth. Nothing else may hand-copy
# a level list; derive from these (or from TestLevel directly).
ALL_LEVELS = list(TestLevel)  # every level, in execution order (== enum order)

# Default when a node's comfy-test.toml omits `levels`. Deliberately excludes:
#   - coverage: opt-in -- it RAISES if a registered node isn't used by any
#     workflow, so it can't be a silent default (would fail existing nodes).
#   - execution_light: redundant with execution (same run, minus per-frame video).
#   - warnings: opt-in -- judgement calls, not conclusive failures. Run it when
#     you want the report; leaving it on by default would make its findings
#     background noise.
DEFAULT_LEVELS = [
    TestLevel.SYNTAX,
    TestLevel.INSTALL,
    TestLevel.REGISTRATION,
    TestLevel.INSTANTIATION,
    TestLevel.STATIC_CAPTURE,
    TestLevel.VALIDATION,
    TestLevel.EXECUTION,
]


@dataclass
class WorkflowConfig:
    """Configuration for workflow testing.

    All workflows in workflows/ and tests/ folders are auto-discovered. Screenshots are always taken.

    Args:
        workflows: All discovered workflow files (auto-populated)
        cpu: Workflows to run on CPU runners (GitHub-hosted). If empty, skip CPU jobs.
        cuda: Workflows to run on CUDA runners (self-hosted). If empty, skip CUDA jobs.
        rocm: Workflows to run on ROCm runners (reserved; no runner wired yet).
        timeout: Timeout in seconds for workflow execution

        # Deprecated fields (for backwards compatibility)
        run: Deprecated - use cpu instead
        screenshot: Deprecated - all workflows are now screenshotted
        files: Deprecated - use workflows folder
        file: Deprecated - use workflows folder
    """

    workflows: List[Path] = field(default_factory=list)
    cpu: List[Path] = field(default_factory=list)
    cuda: List[Path] = field(default_factory=list)
    rocm: List[Path] = field(default_factory=list)
    timeout: int = 3600  # Default 60 minutes

    # Deprecated fields for backwards compatibility
    run: List[Path] = field(default_factory=list)
    screenshot: List[Path] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)
    file: Optional[Path] = None

    def __post_init__(self):
        """Validate and normalize configuration."""
        # Backwards compatibility: migrate deprecated fields to workflows
        if not self.workflows:
            if self.run:
                self.workflows = list(self.run)
            elif self.files:
                self.workflows = list(self.files)
            elif self.file is not None:
                self.workflows = [Path(self.file)]

        # Backwards compatibility: if 'run' specified but no accelerator list, treat as cpu
        if self.run and not self.cpu and not self.cuda and not self.rocm:
            self.cpu = list(self.run)

        # Normalize to Path objects
        self.workflows = [Path(f) for f in self.workflows]
        self.cpu = [Path(f) for f in self.cpu]
        self.cuda = [Path(f) for f in self.cuda]
        self.rocm = [Path(f) for f in self.rocm]
        self.run = [Path(f) for f in self.run]
        self.screenshot = [Path(f) for f in self.screenshot]
        self.files = [Path(f) for f in self.files]

        if self.timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {self.timeout}")


@dataclass
class CoverageConfig:
    """Configuration for the COVERAGE level beyond the node-level check.

    Args:
        inputs: Required input-value coverage, ``{node_type: {input_name:
            [values]}}``. Every listed value must appear as that input's saved
            value on at least one workflow node of that type, across all
            workflow JSONs (litegraph or API format). Parsed from
            ``[test.coverage.inputs]``::

                [test.coverage.inputs]
                MyLoaderNode.model = ["small.safetensors", "large.safetensors"]

            Values must be explicit string lists: combo options are often
            built at runtime (e.g. by scanning a models directory), so the
            universe of required values cannot be derived statically. A
            future "all" form could auto-derive values when the schema's
            options list is a static literal.
    """

    inputs: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)

    def __post_init__(self):
        """Validate declaration shape."""
        if not isinstance(self.inputs, dict):
            raise ValueError(
                f"[test.coverage.inputs] must be a table, got {type(self.inputs).__name__}"
            )
        for node_type, node_inputs in self.inputs.items():
            if not isinstance(node_inputs, dict):
                raise ValueError(
                    f"[test.coverage.inputs] {node_type} must be a table of "
                    f"input_name = [values], got {type(node_inputs).__name__}"
                )
            for input_name, values in node_inputs.items():
                if (
                    not isinstance(values, list)
                    or not values
                    or not all(isinstance(v, str) for v in values)
                ):
                    raise ValueError(
                        f"[test.coverage.inputs] {node_type}.{input_name} must be "
                        f"a non-empty list of strings"
                    )


@dataclass
class PlatformTestConfig:
    """Platform-specific test configuration.

    Args:
        enabled: Whether to run tests on this platform
        skip_workflow: Skip workflow execution (only verify node registration)
        comfyui_portable_version: Version of portable ComfyUI to use (Windows portable only)
    """

    enabled: bool = True
    skip_workflow: bool = False
    comfyui_portable_version: Optional[str] = None


@dataclass
class TestConfig:
    """
    Configuration for installation tests.

    Parsed from comfy-test.toml in the custom node directory.

    Args:
        name: Test suite name (usually node package name)
        comfyui_version: ComfyUI version ("latest", tag, or commit hash)
        python_version: Python version for venv (default: random from 3.11-3.13)
        timeout: Global timeout in seconds for setup operations
        levels: List of test levels to run (install, registration, instantiation, validation)
        workflow: Optional workflow to execute for end-to-end testing
        linux: Linux-specific test configuration
        macos: macOS-specific test configuration
        windows: Windows-specific test configuration
        windows_portable: Windows Portable-specific test configuration

    Example:
        config = TestConfig(
            name="ComfyUI-MyNode",
            levels=[TestLevel.INSTALL, TestLevel.REGISTRATION],
            workflow=WorkflowConfig(run=[Path("basic.json")]),  # Resolved from workflows/
        )
    """

    name: str
    comfyui_version: str = "latest"
    python_version: str = field(default_factory=_default_python_version)
    torch_version: str = DEFAULT_TORCH_VERSION
    # Extra PyPI indexes passed to uv/pip as --extra-index-url (in addition to the
    # built-in PyTorch wheel index + pypi.org). For private mirrors / Artifactory.
    extra_pip_indices: List[str] = field(default_factory=list)
    # Path (relative to the node repo) to a custom test hook: a Python file
    # exposing run(ctx) or check(ctx). Runs as the CUSTOM level (last). None = none.
    custom: Optional[str] = None
    timeout: int = 600
    res: int = 1080  # Viewport height (width = height * 16/9)
    levels: List[TestLevel] = field(default_factory=lambda: list(TestLevel))
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    linux: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    linux_cuda: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    macos: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows_cuda: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows_portable: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows_portable_cuda: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    macos_desktop: PlatformTestConfig = field(default_factory=lambda: PlatformTestConfig(enabled=False))
    windows_desktop: PlatformTestConfig = field(default_factory=lambda: PlatformTestConfig(enabled=False))
    windows_desktop_cuda: PlatformTestConfig = field(default_factory=lambda: PlatformTestConfig(enabled=False))

    def __post_init__(self):
        """Validate configuration."""
        if not self.name:
            raise ValueError("Test name is required")

        # Validate Python version format
        if not self.python_version.replace(".", "").isdigit():
            raise ValueError(f"Invalid Python version: {self.python_version}")

        # Validate timeout
        if self.timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {self.timeout}")

        # Ensure levels are TestLevel enums
        if self.levels:
            self.levels = [
                TestLevel(l) if isinstance(l, str) else l
                for l in self.levels
            ]

        # Ensure workflow is WorkflowConfig
        if isinstance(self.workflow, dict):
            self.workflow = WorkflowConfig(**self.workflow)

        # Ensure coverage is CoverageConfig
        if isinstance(self.coverage, dict):
            self.coverage = CoverageConfig(**self.coverage)


        # Ensure platform configs are PlatformTestConfig
        if isinstance(self.linux, dict):
            self.linux = PlatformTestConfig(**self.linux)
        if isinstance(self.linux_cuda, dict):
            self.linux_cuda = PlatformTestConfig(**self.linux_cuda)
        if isinstance(self.windows, dict):
            self.windows = PlatformTestConfig(**self.windows)
        if isinstance(self.windows_cuda, dict):
            self.windows_cuda = PlatformTestConfig(**self.windows_cuda)
        if isinstance(self.windows_portable, dict):
            self.windows_portable = PlatformTestConfig(**self.windows_portable)
        if isinstance(self.windows_portable_cuda, dict):
            self.windows_portable_cuda = PlatformTestConfig(**self.windows_portable_cuda)
        if isinstance(self.macos_desktop, dict):
            self.macos_desktop = PlatformTestConfig(**self.macos_desktop)
        if isinstance(self.windows_desktop, dict):
            self.windows_desktop = PlatformTestConfig(**self.windows_desktop)
        if isinstance(self.windows_desktop_cuda, dict):
            self.windows_desktop_cuda = PlatformTestConfig(**self.windows_desktop_cuda)

    @property
    def python_short(self) -> str:
        """Get Python version without dots (e.g., '310' for '3.10')."""
        return self.python_version.replace(".", "")

    def get_platform_config(self, platform: str) -> PlatformTestConfig:
        """Get configuration for a specific platform (id or alias).

        The platform taxonomy is single-sourced in comfy_test.platforms; this
        resolves the token there and returns the matching stored config. Imported
        lazily to avoid an import cycle (platforms -> common.config).

        Raises:
            ValueError: If platform is not recognized.
        """
        from ..lanes.registry import resolve
        p = resolve(platform)
        if p is None:
            raise ValueError(f"Unknown platform: {platform}")
        return getattr(self, p.config_key)


def build_provenance(config=None, install_mode: str = "fresh") -> dict:
    """What was ACTUALLY tested -- the fields that make a result reproducible.

    Without these a red cell is uninterpretable and un-rerunnable: the
    interpreter is drawn at random per run (`_random_python_version`), ComfyUI
    is an unpinned shallow HEAD clone, hosted CPU lanes attach to a prebuilt
    cached env while CUDA/local lanes build their own, and comfy-test itself
    floats on `pip install --upgrade`.

    Args:
        config: TestConfig, when available (None on the desktop CDP path,
            which runs outside the orchestrator).
        install_mode: "attach" when the lane prebuilt the env and handed us a
            live server (INSTALL was a no-op, so a green result does NOT mean
            "installs clean"), "fresh" when comfy-test built the venv itself.
    """
    try:
        from importlib.metadata import version as _pkg_version
        comfy_test_version = _pkg_version("comfy-test")
    except Exception:
        comfy_test_version = None

    python_version = getattr(config, "python_version", None) if config else None
    if python_version is None:
        python_version = os.environ.get("COMFY_TEST_PYTHON_VERSION") or (
            f"{sys.version_info.major}.{sys.version_info.minor}")

    torch_version = getattr(config, "torch_version", None) if config else None
    torch_triple = None
    # Only claim a triple when a version was actually requested -- resolving
    # None yields the DEFAULT pin, and reporting that for a run we did not pin
    # (e.g. the Desktop app's bundled torch) would be a lie.
    if torch_version:
        try:
            triple = resolve_torch_triple(torch_version)
            if triple:
                torch_triple = {"torch": triple[0], "torchvision": triple[1],
                                "torchaudio": triple[2]}
        except Exception:
            pass

    levels = None
    if config is not None and getattr(config, "levels", None):
        levels = [getattr(lvl, "value", str(lvl)) for lvl in config.levels]

    return {
        "comfy_test_version": comfy_test_version,
        "python_version": python_version,
        "torch_version": torch_version,
        "torch_triple": torch_triple,
        "install_mode": install_mode,
        "levels": levels,
    }
