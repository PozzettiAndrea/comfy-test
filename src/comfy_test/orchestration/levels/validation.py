"""VALIDATION level - Validate workflows via 4-level validation."""

from pathlib import Path

from ...common.errors import TestError
from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run VALIDATION level.

    Runs 4-level workflow validation on all configured workflows:
    1. Schema - Widget values match allowed enums/types/ranges
    2. Graph - Connections are valid, all nodes exist
    3. Introspection - Node definitions are well-formed
    4. Partial Execution - Run non-CUDA nodes to verify they work

    Args:
        ctx: Level context (must have server set)

    Returns:
        Unchanged context

    Raises:
        TestError: If any workflow fails validation
    """
    workflows = ctx.config.workflow.workflows

    if not workflows:
        ctx.log("No workflows to validate")
        return ctx

    total_workflows = len(workflows)
    ctx.log(f"Validating {total_workflows} workflow(s)...")

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

        ws = WorkflowScreenshot(ctx.server.base_url, log_callback=ctx.log)
        ws.start()
        validation_errors = []
        try:
            for idx, workflow_file in enumerate(workflows, 1):
                ctx.log(f"  [{idx}/{total_workflows}] Validating {workflow_file.name}")
                try:
                    ws.validate_workflow(_resolve_workflow_path(ctx, workflow_file))
                    ctx.log("    OK")
                except Exception as e:
                    ctx.log(f"    FAILED: {e}")
                    validation_errors.append((workflow_file.name, str(e)))
        finally:
            ws.stop()

        if validation_errors:
            raise TestError(
                f"Workflow validation failed ({len(validation_errors)} error(s))",
                "\n".join(f"  - {name}: {err}" for name, err in validation_errors)
            )

    except ImportError:
        ctx.log("WARNING: Validation requires playwright")

    return ctx


def _resolve_workflow_path(ctx: LevelContext, workflow_file: Path) -> Path:
    """Resolve workflow file path relative to node directory."""
    workflow_path = Path(workflow_file)
    if not workflow_path.is_absolute():
        workflow_path = ctx.node_dir / workflow_file
    return workflow_path
