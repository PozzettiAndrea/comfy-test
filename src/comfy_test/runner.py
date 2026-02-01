"""Convenience functions for running tests.

This module provides simple entry points for common testing operations.
"""

from pathlib import Path
from typing import Optional, List

from .common.config import TestConfig
from .common.config_file import discover_config, load_config
from .orchestration.manager import TestManager
from .orchestration.results import TestResult


def run_tests(
    config: Optional[TestConfig | Path | str] = None,
    node_dir: Optional[Path] = None,
    platform: Optional[str] = None,
    dry_run: bool = False,
) -> List[TestResult]:
    """Run installation tests.

    Args:
        config: TestConfig, path to config file, or None to auto-discover
        node_dir: Path to custom node directory (default: current directory)
        platform: Specific platform to test, or None for all enabled
        dry_run: If True, only show what would be done

    Returns:
        List of TestResult

    Example:
        >>> results = run_tests()
        >>> all_passed = all(r.success for r in results)
    """
    # Load config
    if config is None:
        config = discover_config(node_dir)
    elif isinstance(config, (str, Path)):
        config = load_config(config)

    # Create manager
    manager = TestManager(config, node_dir)

    # Run tests
    if platform:
        return [manager.run_platform(platform, dry_run)]
    else:
        return manager.run_all(dry_run)
