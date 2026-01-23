"""Workflow execution and monitoring."""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Callable

from .api import ComfyUIAPI
from .models import WorkflowExecution
from .workflow_converter import WorkflowConverter, set_object_info
from ..errors import WorkflowError


def is_litegraph_format(workflow: Dict[str, Any]) -> bool:
    """Check if workflow is in litegraph format (frontend format)."""
    return not WorkflowConverter.is_api_format(workflow)


def litegraph_to_prompt(
    workflow: Dict[str, Any],
    object_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert litegraph workflow format to ComfyUI prompt format.

    Uses Seth Robinson's battle-tested converter that handles:
    - Subgraphs (including nested)
    - GetNode/SetNode routing
    - PrimitiveNode value injection
    - Reroute passthrough
    - Bypassed/muted nodes
    - And many more edge cases

    Args:
        workflow: Litegraph format workflow (frontend save format)
        object_info: Node definitions from /object_info API

    Returns:
        ComfyUI prompt format (dict of node_id -> node_config)
    """
    # Set the object_info for the converter to use
    set_object_info(object_info)

    # Use the full-featured converter
    return WorkflowConverter.convert_to_api(workflow)


class WorkflowRunner:
    """Runs ComfyUI workflows and monitors their execution.

    Args:
        api: ComfyUIAPI instance connected to running server
        log_callback: Optional callback for logging

    Example:
        >>> runner = WorkflowRunner(api)
        >>> result = runner.run_workflow(Path("workflow.json"), timeout=120)
        >>> print(result.status)
    """

    def __init__(
        self,
        api: ComfyUIAPI,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.api = api
        self._log = log_callback or (lambda msg: print(msg))

    def run_workflow(
        self,
        workflow_file: Path,
        timeout: Optional[int] = None,
    ) -> WorkflowExecution:
        """Run a workflow and wait for completion.

        Args:
            workflow_file: Path to workflow JSON file
            timeout: Maximum seconds to wait for completion (None = no timeout)

        Returns:
            WorkflowExecution with status, outputs, and node tracking

        Raises:
            WorkflowError: If workflow fails or has errors
            TestTimeoutError: If workflow doesn't complete in time
            FileNotFoundError: If workflow file doesn't exist
        """
        workflow_file = Path(workflow_file)
        if not workflow_file.exists():
            raise FileNotFoundError(f"Workflow file not found: {workflow_file}")

        # Load workflow
        self._log(f"Loading workflow from {workflow_file}...")
        with open(workflow_file, "r") as f:
            workflow_data = json.load(f)

        # Extract the prompt (workflow definition)
        # Workflow files can have either just the prompt, or a full structure
        if "prompt" in workflow_data:
            prompt = workflow_data["prompt"]
        elif is_litegraph_format(workflow_data):
            # Convert litegraph format (frontend save) to prompt format (API)
            self._log("Converting litegraph workflow to prompt format...")
            object_info = self.api.get_object_info()
            prompt = litegraph_to_prompt(workflow_data, object_info)
        else:
            prompt = workflow_data

        return self.run_prompt(prompt, timeout, str(workflow_file))

    def run_prompt(
        self,
        prompt: Dict[str, Any],
        timeout: Optional[int] = None,
        workflow_name: str = "workflow",
    ) -> WorkflowExecution:
        """Run a prompt and wait for completion.

        Uses WebSocket for real-time execution tracking.

        Args:
            prompt: Workflow prompt definition
            timeout: Maximum seconds to wait (None = no timeout, defaults to 120)
            workflow_name: Name for logging

        Returns:
            WorkflowExecution with status, outputs, and node tracking
            (runs = nodes that executed, cached = nodes that were cached)

        Raises:
            WorkflowError: If workflow fails
            TestTimeoutError: If workflow doesn't complete in time
        """
        self._log(f"Queuing workflow: {workflow_name}...")

        # Use WebSocket-based execution tracking
        effective_timeout = timeout if timeout is not None else 120
        execution = self.api.execute_workflow(
            prompt,
            timeout=effective_timeout,
            log_callback=self._log,
        )

        # Check for errors
        if execution.has_error:
            error_msg = execution.get_error_message() or "Unknown error"
            raise WorkflowError(
                f"Workflow execution failed: {error_msg}",
                workflow_file=workflow_name,
                node_error=error_msg,
            )

        self._log("Workflow completed successfully!")
        return execution
