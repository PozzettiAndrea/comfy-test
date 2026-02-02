"""Test orchestration for comfy-test.

This module provides the main test orchestration logic for running
ComfyUI custom node installation tests across multiple platforms.
"""

from .manager import TestManager
from .levels.install import get_platform
from .levels.execution import ProgressSpinner
from .results import (
    TestResult,
    TestState,
    save_state,
    load_state,
    has_gpu,
    get_hardware_info,
    get_workflow_timeout,
)

__all__ = [
    "TestManager",
    "get_platform",
    "ProgressSpinner",
    "TestResult",
    "TestState",
    "save_state",
    "load_state",
    "has_gpu",
    "get_hardware_info",
    "get_workflow_timeout",
]
