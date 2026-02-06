"""Custom exceptions for comfy-test."""


class TestError(Exception):
    """Base exception for all comfy-test errors."""

    def __init__(self, message: str, details: str | None = None):
        self.message = message
        self.details = details
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.details:
            return f"{self.message}\n\nDetails:\n{self.details}"
        return self.message


class ConfigError(TestError):
    """Error in configuration file parsing or validation."""

    pass


class SetupError(TestError):
    """Error during ComfyUI or node setup."""

    pass


class ServerError(TestError):
    """Error starting or communicating with ComfyUI server."""

    pass


class VerificationError(TestError):
    """Error during node verification (expected nodes not found)."""

    def __init__(self, message: str, missing_nodes: list[str] | None = None):
        self.missing_nodes = missing_nodes or []
        details = None
        if self.missing_nodes:
            details = f"Missing nodes: {', '.join(self.missing_nodes)}"
        super().__init__(message, details)


class WorkflowError(TestError):
    """Error during workflow execution."""

    def __init__(self, message: str, workflow_file: str | None = None, node_error: str | None = None):
        self.workflow_file = workflow_file
        self.node_error = node_error
        details_parts = []
        if workflow_file:
            details_parts.append(f"Workflow: {workflow_file}")
        if node_error:
            details_parts.append(f"Node error: {node_error}")
        details = "\n".join(details_parts) if details_parts else None
        super().__init__(message, details)


class TestTimeoutError(TestError):
    """Operation timed out."""

    def __init__(self, message: str, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds
        details = f"Timeout: {timeout_seconds} seconds"
        super().__init__(message, details)


class DownloadError(TestError):
    """Error downloading ComfyUI or dependencies."""

    def __init__(self, message: str, url: str | None = None):
        self.url = url
        details = f"URL: {url}" if url else None
        super().__init__(message, details)


class WorkflowValidationError(TestError):
    """Error during workflow validation (schema/graph errors)."""

    def __init__(self, message: str, errors: list | None = None):
        self.validation_errors = errors or []
        details = None
        if self.validation_errors:
            details = "\n".join(str(e) for e in self.validation_errors)
        super().__init__(message, details)


class WorkflowExecutionError(TestError):
    """Error during workflow execution (one or more workflows failed)."""

    def __init__(self, message: str, errors: list | None = None):
        self.execution_errors = errors or []
        details = None
        if self.execution_errors:
            details = "\n".join(str(e) for e in self.execution_errors)
        super().__init__(message, details)
