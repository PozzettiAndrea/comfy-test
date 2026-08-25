"""Level context for passing state between test levels."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Protocol, TYPE_CHECKING

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
    # OS + install method: "linux" | "windows" | "macos" | "windows_portable".
    # This is `platform` in the sys.platform sense -- it is the key
    # levels/install.py:get_platform() dispatches the per-OS class on. It is
    # NOT a lane id: it carries no accelerator.
    platform_name: str
    log: LogCallback
    output_base: Path

    # Optional overrides
    work_dir: Optional[Path] = None  # Custom work directory (default: temp)
    workflow_filter: Optional[str] = None  # Run only this workflow
    # Attach mode: URL of an externally-managed ComfyUI server (CI boots it).
    # When set, INSTALL derives paths instead of building, and REGISTRATION
    # attaches instead of starting a server.
    server_url: Optional[str] = None
    # Lane id this run belongs to (`linux-cuda`, `windows-portable-cpu`).
    # platform_name + accelerator. Recorded in results.json; set by the caller
    # because the desktop path mints its id differently.
    lane_id: Optional[str] = None
    novram: bool = False  # Pass --novram to ComfyUI
    vram_debug: bool = False  # Enable VRAM debug logging

    # Accumulated by levels (None/empty until set)
    platform: Optional["TestPlatform"] = None
    paths: Optional["TestPaths"] = None
    server: Optional["ComfyUIServer"] = None
    api: Optional["ComfyUIAPI"] = None
    registered_nodes: tuple[str, ...] = ()
    cuda_packages: tuple[str, ...] = ()
    # ComfyUI version actually under test. Set from the cloned/extracted tree's
    # pyproject.toml at INSTALL, refined from the live server's /system_stats at
    # REGISTRATION. Written into results.json for provenance.
    comfyui_version: Optional[str] = None
    # ComfyUI's checked-out commit SHA. The pyproject version only moves on
    # releases while we clone HEAD, so this is the field that actually
    # identifies what was tested. None for the portable bundle (no .git).
    comfyui_commit: Optional[str] = None
    env_vars: Optional[dict[str, str]] = None

    def with_updates(self, **kwargs) -> "LevelContext":
        """Return new context with updated fields.

        Example:
            >>> new_ctx = ctx.with_updates(platform=platform, paths=paths)
        """
        return replace(self, **kwargs)


def resolve_lane_id(ctx) -> str:
    """The lane id for a run: explicit if set, else platform_name + accelerator.

    `platform_name` is the OS/install-method half (`windows_portable`); the
    lane adds the accelerator (`windows-portable-cuda`). Hyphenated, matching
    the on-disk directory and the registry id.
    """
    if ctx.lane_id:
        return ctx.lane_id
    from ..backends import active_backend_name
    return f"{ctx.platform_name.replace('_', '-')}-{active_backend_name()}"


def require_workflows(ctx, level_name: str) -> None:
    """Hard-fail a runtime level when the pack ships no workflows at all.

    Asking for `execution` and shipping zero workflows used to log one line and
    return green: the level that is supposed to prove your nodes run had
    nothing to run, and the badge said pass. A level you listed is a level you
    asserted; if it cannot execute, that is a red.

    `workflow.workflows` is every auto-discovered file, not the per-accelerator
    selection -- so this fires only when the pack has no workflows anywhere,
    never because a lane's `cpu`/`cuda` list happens to be empty.

    `skip_workflow` remains the one supported way to say "run the pipeline but
    not the workflows" on a lane.
    """
    from ..common.errors import ConfigError

    if ctx.config.workflow.workflows:
        return
    if ctx.config.get_platform_config(ctx.platform_name).skip_workflow:
        return
    raise ConfigError(
        f"level '{level_name}' is enabled but this pack ships no workflows.\n"
        f"comfy-test auto-discovers them from the folders ComfyUI recognises "
        f"(example_workflows/, example/, examples/, workflow/, workflows/); "
        f"none were found.\n"
        f"Fix it one of three ways:\n"
        f"  - add a workflow .json to example_workflows/\n"
        f"  - drop '{level_name}' from [test] levels in comfy-test.toml\n"
        f"  - set skip_workflow = true under [test.{ctx.platform_name}] "
        f"to skip workflows on this lane only"
    )
