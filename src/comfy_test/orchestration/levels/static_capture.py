"""STATIC_CAPTURE level - Capture static screenshots of workflows."""

from pathlib import Path

from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run STATIC_CAPTURE level.

    Takes static screenshots of all configured workflows without executing them.
    Requires playwright to be installed.

    Args:
        ctx: Level context (must have server set)

    Returns:
        Unchanged context

    Raises:
        ImportError: If playwright is not installed (gracefully skipped)
    """
    workflows = ctx.config.workflow.workflows

    if not workflows:
        ctx.log("No workflows configured for static capture")
        return ctx

    total_screenshots = len(workflows)
    ctx.log(f"Capturing {total_screenshots} static screenshot(s)...")

    try:
        from ...reporting.screenshot import (
            WorkflowScreenshot,
            check_dependencies,
            ensure_dependencies,
        )

        # Auto-install playwright if missing
        if not ensure_dependencies(log_callback=ctx.log):
            raise ImportError("Failed to install screenshot dependencies")
        check_dependencies()

        screenshots_dir = ctx.output_base / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        ws = WorkflowScreenshot(ctx.server.base_url, log_callback=ctx.log)
        ws.start()
        try:
            for idx, workflow_file in enumerate(workflows, 1):
                ctx.log(f"  [{idx}/{total_screenshots}] STATIC {workflow_file.name}")
                output_path = screenshots_dir / f"{workflow_file.stem}.png"
                ws.capture(_resolve_workflow_path(ctx, workflow_file), output_path=output_path)
        finally:
            ws.stop()

    except ImportError:
        ctx.log("WARNING: Screenshots disabled (playwright not installed)")

    return ctx


def _resolve_workflow_path(ctx: LevelContext, workflow_file: Path) -> Path:
    """Resolve workflow file path relative to node directory."""
    workflow_path = Path(workflow_file)
    if not workflow_path.is_absolute():
        workflow_path = ctx.node_dir / workflow_file
    return workflow_path
