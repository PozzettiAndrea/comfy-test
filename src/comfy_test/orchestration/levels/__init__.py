"""Test level implementations.

Each level is a function that takes a LevelContext and returns an updated LevelContext.
"""

from .syntax import run as run_syntax
from .coverage import run as run_coverage
from .install import run as run_install
from .registration import run as run_registration
from .javascript import run as run_javascript
from .instantiation import run as run_instantiation
from .static_capture import run as run_static_capture
from .validation import run as run_validation
from .execution_light import run as run_execution_light
from .execution import run as run_execution
from .custom import run as run_custom

__all__ = [
    "run_syntax",
    "run_coverage",
    "run_install",
    "run_registration",
    "run_javascript",
    "run_instantiation",
    "run_static_capture",
    "run_validation",
    "run_execution_light",
    "run_execution",
    "run_custom",
]
