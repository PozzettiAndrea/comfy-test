"""Level context for passing state between test levels."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..common.config import TestConfig
    from ..common.base_platform import TestPlatform, TestPaths
    from ..comfyui.server import ComfyUIServer
    from ..comfyui.api import ComfyUIAPI


class LogCallback(Protocol):
    """Protocol for logging callbacks."""

    def __call__(self, msg: str) -> None: ...


@dataclass(frozen=True)
class LevelContext:
    """Immutable context passed to each level.

    Levels receive this context and return an updated context with any
    new state they've accumulated. This makes data flow between levels
    explicit and testable.

    Example:
        >>> ctx = LevelContext(config=config, node_dir=Path("."), ...)
        >>> ctx = run_install(ctx)  # Returns ctx with platform, paths set
        >>> ctx = run_registration(ctx)  # Returns ctx with server, api set
    """

    # Required fields (set at start)
    config: "TestConfig"
    node_dir: Path
    platform_name: str
    log: LogCallback
    output_base: Path

    # Optional overrides
    work_dir: Optional[Path] = None  # Custom work directory (default: temp)
    comfyui_dir: Optional[Path] = None  # Existing ComfyUI to use
    server_url: Optional[str] = None  # External server URL
    workflow_filter: Optional[str] = None  # Run only this workflow
    deps_installed: bool = False  # Skip requirements.txt and install.py

    # Accumulated by levels (None/empty until set)
    platform: Optional["TestPlatform"] = None
    paths: Optional["TestPaths"] = None
    server: Optional["ComfyUIServer"] = None
    api: Optional["ComfyUIAPI"] = None
    registered_nodes: tuple[str, ...] = ()
    cuda_packages: tuple[str, ...] = ()
    env_vars: Optional[dict[str, str]] = None

    def with_updates(self, **kwargs) -> "LevelContext":
        """Return new context with updated fields.

        Example:
            >>> new_ctx = ctx.with_updates(platform=platform, paths=paths)
        """
        return replace(self, **kwargs)
