"""EXECUTION level - run every configured workflow and record video of each.

The terminal level: it drives a real browser over the ComfyUI canvas, captures
per-frame video while the graph executes, and writes results.json plus the HTML
report.

The body is shared with EXECUTION_LIGHT (`_workflow_run.py`) -- the two differ
only in whether the browser drives the run. They were two forked copies until
three fixes landed in one and not the other; see that module's docstring.
"""

from ..context import LevelContext
from ._workflow_run import ProgressSpinner, _resolve_workflow_path, run_workflows

__all__ = ["run", "ProgressSpinner", "_resolve_workflow_path"]


def run(ctx: LevelContext) -> LevelContext:
    """Run EXECUTION: every workflow, with per-frame video capture.

    Args:
        ctx: Level context (must have server, api set)

    Returns:
        Updated context

    Raises:
        WorkflowExecutionError: If any workflow fails
    """
    return run_workflows(ctx, capture=True, level_name="execution")
