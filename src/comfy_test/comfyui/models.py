"""Data models for ComfyUI workflow execution."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowExecution:
    """Tracks execution state of a workflow.

    Provides detailed tracking of which nodes executed, which were cached,
    and any errors that occurred during execution.

    Attributes:
        prompt_id: The prompt ID returned by queue_prompt
        runs: Set of node IDs that actually executed
        cached: Set of node IDs that were cached (skipped execution)
        outputs: Dictionary of node outputs keyed by node ID
        error: Error details dict if execution failed, None otherwise

    Example:
        >>> execution = WorkflowExecution(prompt_id="abc123")
        >>> execution.runs.add("5")
        >>> execution.cached.add("3")
        >>> execution.was_executed("5")  # True - it ran
        >>> execution.was_executed("3")  # True - it was cached
        >>> execution.was_executed("9")  # False - never touched
    """

    prompt_id: str
    runs: set[str] = field(default_factory=set)
    cached: set[str] = field(default_factory=set)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def did_run(self, node_id: str) -> bool:
        """Check if a node actually executed (not cached)."""
        return node_id in self.runs

    def was_cached(self, node_id: str) -> bool:
        """Check if a node was cached (skipped execution)."""
        return node_id in self.cached

    def was_executed(self, node_id: str) -> bool:
        """Check if a node was executed (either ran or was cached)."""
        return self.did_run(node_id) or self.was_cached(node_id)

    @property
    def has_error(self) -> bool:
        """Check if execution encountered an error."""
        return self.error is not None

    @property
    def status(self) -> str:
        """Get execution status string."""
        if self.has_error:
            return "error"
        return "success"

    def get_error_message(self) -> str | None:
        """Get formatted error message if execution failed."""
        if not self.error:
            return None

        exc_type = self.error.get("exception_type", "Error")
        exc_msg = self.error.get("exception_message", "Unknown error")
        node_id = self.error.get("node_id", "unknown")

        return f"{exc_type} in node {node_id}: {exc_msg}"
