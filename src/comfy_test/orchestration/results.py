"""Test results and state management for comfy-test orchestration."""

import json
import os
import platform
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List

from ..common.errors import TestError


@dataclass
class TestState:
    """State persisted between CLI invocations for multi-step CI.

    This allows running test levels in separate CI steps while sharing
    the ComfyUI environment setup between them.
    """
    comfyui_dir: str
    python: str
    custom_nodes_dir: str
    cuda_packages: List[str]
    platform_name: str


def save_state(state: TestState, work_dir: Path) -> None:
    """Save test state to work directory for later resumption."""
    state_file = work_dir / "state.json"
    work_dir.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding='utf-8') as f:
        json.dump(asdict(state), f, indent=2)


def load_state(work_dir: Path) -> TestState:
    """Load test state from work directory."""
    state_file = work_dir / "state.json"
    if not state_file.exists():
        raise TestError(
            "No state file found",
            f"Expected {state_file}. Run install level first with --work-dir."
        )
    with open(state_file, encoding='utf-8-sig') as f:
        data = json.load(f)
    return TestState(**data)


class TestResult:
    """Result of a test run.

    Attributes:
        platform: Platform name
        success: Whether the test passed
        error: Error message if failed
        details: Additional details
    """

    def __init__(
        self,
        platform: str,
        success: bool,
        error: Optional[str] = None,
        details: Optional[str] = None,
    ):
        self.platform = platform
        self.success = success
        self.error = error
        self.details = details

    def __repr__(self) -> str:
        status = "PASS" if self.success else "FAIL"
        return f"TestResult({self.platform}: {status})"


def has_gpu() -> bool:
    """Check if a GPU is available for CUDA operations.

    Returns:
        True if nvidia-smi succeeds (GPU available), False otherwise
    """
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_hardware_info() -> dict:
    """Get current hardware information for results tracking."""
    info = {
        "os": platform.platform(),
        "cpu": platform.processor() or "Unknown",
    }

    # Get better CPU info on Linux
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":")[1].strip()
                    break
    except (FileNotFoundError, PermissionError):
        pass

    # Get GPU info
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            info["gpu"] = result.stdout.strip().split("\n")[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return info


def get_workflow_timeout(config_timeout: int) -> int:
    """Get workflow timeout, using very long timeout for GPU mode."""
    if os.environ.get("COMFY_TEST_GPU"):
        # GPU mode: use 24 hours (effectively no timeout)
        return 86400
    return config_timeout
