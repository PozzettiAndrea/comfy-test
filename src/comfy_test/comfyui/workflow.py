"""Workflow execution and monitoring."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Callable

from .api import ComfyUIAPI
from .workflow_converter import WorkflowConverter, set_object_info
from ..errors import WorkflowError, TestTimeoutError


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
        >>> print(result["status"])
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
    ) -> Dict[str, Any]:
        """Run a workflow and wait for completion.

        Args:
            workflow_file: Path to workflow JSON file
            timeout: Maximum seconds to wait for completion (None = no timeout)

        Returns:
            Execution result with status and outputs

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
    ) -> Dict[str, Any]:
        """Run a prompt and wait for completion.

        Args:
            prompt: Workflow prompt definition
            timeout: Maximum seconds to wait (None = no timeout)
            workflow_name: Name for logging

        Returns:
            Execution result

        Raises:
            WorkflowError: If workflow fails
            TestTimeoutError: If workflow doesn't complete in time
        """
        self._log(f"Queuing workflow: {workflow_name}...")

        # Queue the prompt
        prompt_id = self.api.queue_prompt(prompt)
        self._log(f"Queued with ID: {prompt_id}")

        # Wait for completion
        return self._wait_for_completion(prompt_id, timeout, workflow_name)

    def _wait_for_completion(
        self,
        prompt_id: str,
        timeout: Optional[int],
        workflow_name: str,
    ) -> Dict[str, Any]:
        """Wait for workflow to complete.

        Args:
            prompt_id: ID of queued prompt
            timeout: Maximum seconds to wait (None = no timeout)
            workflow_name: Name for error messages

        Returns:
            Execution result

        Raises:
            WorkflowError: If workflow fails
            TestTimeoutError: If workflow doesn't complete
        """
        if timeout is not None:
            self._log(f"Waiting for workflow completion (timeout: {timeout}s)...")
        else:
            self._log("Waiting for workflow completion...")

        start_time = time.time()

        while True:
            # Check timeout if specified
            if timeout is not None and time.time() - start_time >= timeout:
                self.api.interrupt()
                raise TestTimeoutError(
                    f"Workflow did not complete within {timeout} seconds",
                    timeout_seconds=timeout,
                )

            history = self.api.get_history(prompt_id)

            if history is None:
                # Not started yet, check queue
                queue = self.api.get_queue()
                pending = len(queue.get("queue_pending", []))
                running = len(queue.get("queue_running", []))
                self._log(f"  Queue: {running} running, {pending} pending")
                time.sleep(2)
                continue

            # Check for completion
            status = history.get("status", {})
            status_str = status.get("status_str", "")

            if status_str == "success":
                self._log("Workflow completed successfully!")
                return {
                    "status": "success",
                    "prompt_id": prompt_id,
                    "outputs": history.get("outputs", {}),
                }

            if status_str == "error":
                # Extract error details
                messages = status.get("messages", [])
                error_msg = self._format_error_messages(messages)
                raise WorkflowError(
                    f"Workflow execution failed: {error_msg}",
                    workflow_file=workflow_name,
                    node_error=error_msg,
                )

            # Check for node errors in execution
            if "outputs" in history:
                for node_id, output in history["outputs"].items():
                    if isinstance(output, dict) and output.get("error"):
                        raise WorkflowError(
                            f"Node {node_id} failed",
                            workflow_file=workflow_name,
                            node_error=str(output.get("error")),
                        )

            # Still running
            elapsed = int(time.time() - start_time)
            self._log(f"  Running... ({elapsed}s elapsed)")
            time.sleep(2)

    def _format_error_messages(self, messages: list) -> str:
        """Format error messages from workflow execution."""
        if not messages:
            return "Unknown error"

        formatted = []
        for msg in messages:
            if isinstance(msg, dict):
                formatted.append(msg.get("message", str(msg)))
            else:
                formatted.append(str(msg))

        return "; ".join(formatted)
