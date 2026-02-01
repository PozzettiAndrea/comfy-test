"""ComfyUI REST API client."""

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket

from ..common.errors import ServerError, TestTimeoutError, VerificationError
from .models import WorkflowExecution


class ComfyUIAPI:
    """Client for ComfyUI REST API.

    Provides methods to interact with a running ComfyUI server.

    Args:
        base_url: Base URL of the ComfyUI server (e.g., "http://127.0.0.1:8188")
        timeout: Request timeout in seconds

    Example:
        >>> api = ComfyUIAPI("http://127.0.0.1:8188")
        >>> nodes = api.get_object_info()
        >>> print(list(nodes.keys())[:5])
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def health_check(self) -> bool:
        """Check if the server is responsive.

        Returns:
            True if server responds, False otherwise
        """
        try:
            response = self.session.get(
                f"{self.base_url}/system_stats",
                timeout=self.timeout,
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def get_object_info(self) -> Dict[str, Any]:
        """Get information about all registered nodes.

        Returns:
            Dictionary mapping node names to their info

        Raises:
            ServerError: If request fails
        """
        try:
            response = self.session.get(
                f"{self.base_url}/object_info",
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ServerError(
                "Failed to get object_info from ComfyUI",
                str(e)
            )

    def verify_nodes(self, expected_nodes: List[str]) -> None:
        """Verify that expected nodes are registered.

        Args:
            expected_nodes: List of node names that must exist

        Raises:
            VerificationError: If any expected nodes are missing
        """
        nodes = self.get_object_info()
        missing = [name for name in expected_nodes if name not in nodes]

        if missing:
            raise VerificationError(
                f"Expected nodes not found: {', '.join(missing)}",
                missing_nodes=missing,
            )

    def validate_prompt(self, prompt: Dict[str, Any]) -> None:
        """Validate a workflow without queueing it for execution.

        Requires ComfyUI-validate-endpoint custom node to be installed.
        See: https://github.com/PozzettiAndrea/ComfyUI-validate-endpoint

        Args:
            prompt: Workflow definition in API format (dict of node_id -> node_config)

        Raises:
            ServerError: If validation fails or endpoint not available
        """
        try:
            response = self.session.post(
                f"{self.base_url}/validate",
                json={"prompt": prompt},
                timeout=self.timeout,
            )
            if response.status_code == 400:
                data = response.json()
                error = data.get("error", {})
                node_errors = data.get("node_errors", {})
                details = error.get("message", "Unknown validation error")
                if error.get("details"):
                    details += f"\n{error['details']}"
                if node_errors:
                    details += f"\nNode errors:\n{json.dumps(node_errors, indent=2)}"
                raise ServerError("Workflow validation failed", details)
            response.raise_for_status()
        except requests.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 404:
                    raise ServerError(
                        "Validation endpoint not available",
                        "Install ComfyUI-validate-endpoint: https://github.com/PozzettiAndrea/ComfyUI-validate-endpoint"
                    )
            raise ServerError("Failed to validate prompt", str(e))

    def queue_prompt(self, workflow: Dict[str, Any]) -> str:
        """Queue a workflow for execution.

        Args:
            workflow: Workflow definition (the "prompt" part of a workflow JSON)

        Returns:
            Prompt ID for tracking execution

        Raises:
            ServerError: If request fails
        """
        try:
            response = self.session.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["prompt_id"]
        except requests.RequestException as e:
            # Try to extract validation error details from response
            details = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    if 'node_errors' in error_data:
                        # Format node errors for readability
                        details = f"Node validation errors:\n{json.dumps(error_data['node_errors'], indent=2)}"
                    elif 'error' in error_data:
                        error_info = error_data['error']
                        details = error_info.get('message', str(error_info))
                except Exception:
                    pass  # Keep original error string
            raise ServerError(
                "Failed to queue prompt",
                details
            )
        except KeyError:
            raise ServerError(
                "Invalid response from /prompt endpoint",
                "Missing prompt_id in response"
            )

    def get_history(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        """Get execution history for a prompt.

        Args:
            prompt_id: ID from queue_prompt

        Returns:
            History data if available, None if prompt hasn't started

        Raises:
            ServerError: If request fails
        """
        try:
            response = self.session.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data.get(prompt_id)
        except requests.RequestException as e:
            raise ServerError(
                f"Failed to get history for prompt {prompt_id}",
                str(e)
            )

    def get_queue(self) -> Dict[str, Any]:
        """Get current queue status.

        Returns:
            Queue data with 'queue_running' and 'queue_pending'

        Raises:
            ServerError: If request fails
        """
        try:
            response = self.session.get(
                f"{self.base_url}/queue",
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ServerError(
                "Failed to get queue status",
                str(e)
            )

    def interrupt(self) -> None:
        """Interrupt currently running workflow."""
        try:
            self.session.post(
                f"{self.base_url}/interrupt",
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass  # Best effort

    def free_memory(self, unload_models: bool = True) -> None:
        """Free memory and optionally unload models.

        Calls ComfyUI's /free endpoint to release cached data.
        This helps prevent memory accumulation when running multiple workflows.

        Args:
            unload_models: If True, also unload any loaded models
        """
        try:
            self.session.post(
                f"{self.base_url}/free",
                json={"unload_models": unload_models, "free_memory": True},
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass  # Best effort - don't fail if cleanup fails

    def _get_ws_url(self, client_id: str) -> str:
        """Get WebSocket URL for the server."""
        # Convert http(s) to ws(s)
        if self.base_url.startswith("https://"):
            ws_base = "wss://" + self.base_url[8:]
        else:
            ws_base = "ws://" + self.base_url[7:]
        return f"{ws_base}/ws?clientId={client_id}"

    def execute_workflow(
        self,
        workflow: Dict[str, Any],
        timeout: int = 120,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> WorkflowExecution:
        """Execute workflow and track completion via WebSocket.

        Uses WebSocket for real-time execution tracking instead of polling.
        This provides lower latency and detailed information about which
        nodes executed vs. were cached.

        Args:
            workflow: Workflow definition (the "prompt" part of a workflow JSON)
            timeout: Maximum seconds to wait for completion
            log_callback: Optional callback for progress logging

        Returns:
            WorkflowExecution with detailed execution state

        Raises:
            ServerError: If connection or execution fails
            TestTimeoutError: If workflow doesn't complete in time
        """
        log = log_callback or (lambda msg: None)
        client_id = str(uuid.uuid4())

        # Connect WebSocket
        ws_url = self._get_ws_url(client_id)
        log(f"[DEBUG] Connecting WebSocket to {ws_url}")
        try:
            ws = websocket.WebSocket()
            ws.connect(ws_url)
            log(f"[DEBUG] WebSocket connected successfully")
        except (ConnectionRefusedError, OSError, websocket.WebSocketException) as e:
            raise ServerError(
                "Failed to connect WebSocket for execution tracking",
                str(e)
            )

        try:
            # Queue the prompt
            prompt_id = self.queue_prompt(workflow)
            execution = WorkflowExecution(prompt_id=prompt_id)
            log(f"Queued with ID: {prompt_id}")

            start_time = time.time()

            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    log(f"[DEBUG] TIMEOUT reached at {elapsed:.1f}s (limit={timeout}s)")
                    self.interrupt()
                    raise TestTimeoutError(
                        f"Workflow did not complete within {timeout} seconds",
                        timeout_seconds=timeout,
                    )

                # Set socket timeout for this recv
                ws.settimeout(min(5.0, timeout - elapsed))

                try:
                    message_data = ws.recv()
                except websocket.WebSocketTimeoutException:
                    # Timeout on recv - check history as fallback in case we missed completion
                    log(f"[DEBUG] WS recv timeout at {elapsed:.1f}s, checking history fallback...")
                    history = self.get_history(prompt_id)
                    if history:
                        log(f"[DEBUG] History found during timeout - workflow completed!")
                        log("Execution complete (detected via history fallback)")
                        execution.outputs = history.get("outputs", {})
                        break
                    continue

                if not isinstance(message_data, str):
                    # Binary data (e.g., preview images), skip
                    log(f"[DEBUG] WS recv binary data ({len(message_data)} bytes), skipping")
                    continue

                try:
                    message = json.loads(message_data)
                except json.JSONDecodeError:
                    log(f"[DEBUG] WS recv invalid JSON: {message_data[:100]}")
                    continue

                msg_type = message.get("type")
                data = message.get("data", {})
                msg_prompt_id = data.get("prompt_id")

                # Log ALL messages for debugging
                log(f"[DEBUG] WS msg: type={msg_type} prompt_id={msg_prompt_id} (waiting for {prompt_id})")

                # Only process messages for our prompt
                if msg_prompt_id and msg_prompt_id != prompt_id:
                    log(f"[DEBUG] Skipping msg - prompt_id mismatch")
                    continue

                if msg_type == "executing":
                    node_id = data.get("node")
                    if node_id is None:
                        # Execution complete (node=None signals end)
                        log(f"[DEBUG] Got 'executing' with node=None -> COMPLETION SIGNAL")
                        log("Execution complete")
                        break
                    execution.runs.add(node_id)
                    log(f"  Executing node: {node_id}")

                elif msg_type == "execution_cached":
                    cached_nodes = data.get("nodes", [])
                    execution.cached.update(cached_nodes)
                    if cached_nodes:
                        log(f"  Cached nodes: {', '.join(cached_nodes)}")

                elif msg_type == "execution_error":
                    # Capture full error details
                    execution.error = data
                    log(f"[DEBUG] Got 'execution_error' -> breaking")
                    log(f"  Execution error: {data.get('exception_type', 'Unknown')}")
                    break

                elif msg_type == "execution_success":
                    log(f"[DEBUG] Got 'execution_success' -> breaking")
                    log("Execution complete (success)")
                    break

                elif msg_type == "status":
                    # Check queue_remaining for completion signal
                    status_data = data.get("status", {})
                    queue_remaining = status_data.get("exec_info", {}).get("queue_remaining", -1)
                    # If queue empty, check history to confirm our prompt completed
                    if queue_remaining == 0:
                        history = self.get_history(prompt_id)
                        if history:
                            log("Execution complete (queue empty)")
                            execution.outputs = history.get("outputs", {})
                            break

                else:
                    log(f"[DEBUG] Unhandled msg_type: {msg_type}")

            # Fetch outputs from history
            history = self.get_history(prompt_id)
            if history:
                execution.outputs = history.get("outputs", {})

            return execution

        finally:
            ws.close()

    def close(self) -> None:
        """Close the session."""
        self.session.close()

    def __enter__(self) -> "ComfyUIAPI":
        return self

    def __exit__(self, *args) -> None:
        self.close()
