"""Guard: test levels are single-sourced from the TestLevel enum.

Prevents regressing the "'all' silently drops coverage" bug — the level sets
must derive from the enum, never a hand-copied literal.
"""

import tempfile
from pathlib import Path

from comfy_test.common.config import TestLevel, ALL_LEVELS, DEFAULT_LEVELS
from comfy_test.common.config_file import load_config


def _cfg(body: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "comfy-test.toml").write_text(
        body + '\n[test.platforms]\nplatforms = ["linux-cpu"]\n')
    return d / "comfy-test.toml"


def test_all_levels_is_the_whole_enum():
    assert ALL_LEVELS == list(TestLevel)


def test_all_means_all_including_coverage():
    levels = load_config(_cfg('[test]\nlevels = "all"')).levels
    assert set(levels) == set(TestLevel), "'all' must include every level"
    assert TestLevel.COVERAGE in levels  # the level that used to be dropped


def test_default_is_intentional_subset():
    # coverage (can fail) + execution_light (redundant with execution) stay opt-in.
    assert TestLevel.COVERAGE not in DEFAULT_LEVELS
    assert TestLevel.EXECUTION_LIGHT not in DEFAULT_LEVELS
    assert load_config(_cfg("[test]")).levels == DEFAULT_LEVELS


if __name__ == "__main__":
    test_all_levels_is_the_whole_enum()
    test_all_means_all_including_coverage()
    test_default_is_intentional_subset()
    print("ok  levels single-sourced; 'all' includes coverage")
