"""Test level implementations.

Each level is a function that takes a LevelContext and returns an updated LevelContext.
"""

from .syntax import run as run_syntax
from .install import run as run_install
from .registration import run as run_registration
from .instantiation import run as run_instantiation
from .static_capture import run as run_static_capture
from .validation import run as run_validation
from .execution import run as run_execution

__all__ = [
    "run_syntax",
    "run_install",
    "run_registration",
    "run_instantiation",
    "run_static_capture",
    "run_validation",
    "run_execution",
]
