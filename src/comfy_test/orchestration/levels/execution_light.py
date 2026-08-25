"""EXECUTION_LIGHT level - run every workflow, one screenshot each, no video.

Same execution as EXECUTION, minus the per-frame video capture. That capture
loop pegs the browser process at 100% CPU on weak runners (macos-cpu, 7 GB) and
the Playwright IPC pipe eventually dies. Here the workflow runs server-side via
WebSocket polling with the browser idle, then one screenshot is taken at the
end.

The body is shared with EXECUTION (`_workflow_run.py`); this module only picks
`capture=False`.
"""

from ..context import LevelContext
from ._workflow_run import _resolve_workflow_path, run_workflows

__all__ = ["run", "_resolve_workflow_path"]


def run(ctx: LevelContext) -> LevelContext:
    """Run EXECUTION_LIGHT: every workflow, one static screenshot each.

    Args:
        ctx: Level context (must have server, api set)

    Returns:
        Updated context

    Raises:
        WorkflowExecutionError: If any workflow fails
    """
    return run_workflows(ctx, capture=False, level_name="execution_light")
