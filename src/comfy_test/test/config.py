"""Configuration dataclasses for installation tests."""

import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, List

# Supported Python versions for random selection
PYTHON_VERSIONS = ["3.11", "3.12", "3.13"]


def _random_python_version() -> str:
    """Select a random Python version for testing."""
    return random.choice(PYTHON_VERSIONS)


class TestLevel(str, Enum):
    """Test levels - each is explicit, run what's in the list.

    - syntax: Check project structure (pyproject.toml vs requirements.txt)
    - install: Setup ComfyUI, install node, install deps
    - registration: Start server, check nodes in object_info (requires install)
    - instantiation: Call each node's constructor (requires install)
    - static_capture: Take static screenshots of workflows (requires install)
    - validation: Validate workflows via /validate endpoint (requires install)
    - execution: Run workflows end-to-end, capture with outputs (requires install, may require GPU)

    Dependencies:
    - syntax: standalone
    - install: standalone
    - registration, instantiation, static_capture, validation, execution: all require install
    """
    SYNTAX = "syntax"
    INSTALL = "install"
    REGISTRATION = "registration"
    INSTANTIATION = "instantiation"
    STATIC_CAPTURE = "static_capture"
    VALIDATION = "validation"
    EXECUTION = "execution"

    @classmethod
    def get_dependencies(cls, level: "TestLevel") -> List["TestLevel"]:
        """Get levels that must run before this level.

        Returns:
            List of prerequisite levels (not including the level itself)
        """
        deps = {
            cls.SYNTAX: [],
            cls.INSTALL: [],
            cls.REGISTRATION: [cls.INSTALL],
            cls.INSTANTIATION: [cls.INSTALL],
            cls.STATIC_CAPTURE: [cls.INSTALL],
            cls.VALIDATION: [cls.INSTALL],
            cls.EXECUTION: [cls.INSTALL],
        }
        return deps.get(level, [])

    @classmethod
    def resolve_dependencies(cls, levels: List["TestLevel"]) -> List["TestLevel"]:
        """Add any missing dependencies to a list of levels.

        Args:
            levels: Levels the user wants to run

        Returns:
            Levels with dependencies added, in execution order
        """
        all_levels = set(levels)
        for level in levels:
            for dep in cls.get_dependencies(level):
                all_levels.add(dep)

        # Return in execution order
        order = [cls.SYNTAX, cls.INSTALL, cls.REGISTRATION, cls.INSTANTIATION, cls.STATIC_CAPTURE, cls.VALIDATION, cls.EXECUTION]
        return [l for l in order if l in all_levels]


@dataclass
class WorkflowConfig:
    """Configuration for workflow testing.

    All workflows in workflows/ folder are auto-discovered. Screenshots are always taken.

    Args:
        workflows: All discovered workflow files (auto-populated)
        gpu: Workflows that require GPU to execute (skipped on non-GPU environments).
             Can be set to all workflows or a subset.
        timeout: Timeout in seconds for workflow execution

        # Deprecated fields (for backwards compatibility)
        run: Deprecated - all workflows are now run
        screenshot: Deprecated - all workflows are now screenshotted
        files: Deprecated - use workflows folder
        file: Deprecated - use workflows folder
    """

    workflows: List[Path] = field(default_factory=list)
    gpu: List[Path] = field(default_factory=list)
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

        # Normalize to Path objects
        self.workflows = [Path(f) for f in self.workflows]
        self.gpu = [Path(f) for f in self.gpu]
        self.run = [Path(f) for f in self.run]
        self.screenshot = [Path(f) for f in self.screenshot]
        self.files = [Path(f) for f in self.files]

        if self.timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {self.timeout}")


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
    python_version: str = field(default_factory=_random_python_version)
    timeout: int = 600
    levels: List[TestLevel] = field(default_factory=lambda: list(TestLevel))
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    linux: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    macos: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows: PlatformTestConfig = field(default_factory=PlatformTestConfig)
    windows_portable: PlatformTestConfig = field(default_factory=PlatformTestConfig)

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

        # Ensure platform configs are PlatformTestConfig
        if isinstance(self.linux, dict):
            self.linux = PlatformTestConfig(**self.linux)
        if isinstance(self.windows, dict):
            self.windows = PlatformTestConfig(**self.windows)
        if isinstance(self.windows_portable, dict):
            self.windows_portable = PlatformTestConfig(**self.windows_portable)

    @property
    def python_short(self) -> str:
        """Get Python version without dots (e.g., '310' for '3.10')."""
        return self.python_version.replace(".", "")

    def get_platform_config(self, platform: str) -> PlatformTestConfig:
        """Get configuration for a specific platform.

        Args:
            platform: Platform name ('linux', 'macos', 'windows', 'windows_portable')

        Returns:
            PlatformTestConfig for the specified platform

        Raises:
            ValueError: If platform is not recognized
        """
        platform_map = {
            "linux": self.linux,
            "macos": self.macos,
            "windows": self.windows,
            "windows_portable": self.windows_portable,
            "windows-portable": self.windows_portable,  # Allow hyphen variant
        }
        if platform not in platform_map:
            raise ValueError(f"Unknown platform: {platform}")
        return platform_map[platform]
