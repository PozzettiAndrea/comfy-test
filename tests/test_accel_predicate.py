"""One predicate for "is this a CUDA run", and a guard that keeps it that way.

`COMFY_TEST_CUDA` is set to the *string* `"0"` on every CPU lane, so a bare
truthiness test is True there. Two sites got that wrong at once -- the
per-workflow timeout became 24 hours on all six hosted lanes, and the torch
triple resolved against the CUDA wheel index while the install pulled from the
CPU one. Five different spellings of the question were in the tree.

This suite pins the semantics and fails if a raw read reappears.
"""
import os
import re
from pathlib import Path

import pytest

from comfy_test.common.accel import accel_name, is_cuda_run

_SRC = Path(__file__).resolve().parent.parent / "src" / "comfy_test"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("yes", True), ("True", True),
    ("0", False), ("", False), ("false", False), ("no", False), ("NO", False),
])
def test_predicate_semantics(monkeypatch, value, expected):
    monkeypatch.setenv("COMFY_TEST_CUDA", value)
    assert is_cuda_run() is expected
    assert accel_name() == ("cuda" if expected else "cpu")


def test_unset_is_cpu(monkeypatch):
    monkeypatch.delenv("COMFY_TEST_CUDA", raising=False)
    assert is_cuda_run() is False
    assert accel_name() == "cpu"


def test_the_string_zero_is_not_truthy(monkeypatch):
    """The exact bug. Every CPU lane sets "0", which is a non-empty string."""
    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    assert bool(os.environ.get("COMFY_TEST_CUDA")) is True   # the trap
    assert is_cuda_run() is False                            # the fix


def test_cpu_lane_stamps_a_cpu_lane_id(monkeypatch):
    """Every CPU lane used to write results.json claiming it ran on CUDA."""
    from types import SimpleNamespace

    from comfy_test.orchestration.context import resolve_lane_id
    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    monkeypatch.delenv("COMFY_TEST_BACKEND", raising=False)
    for platform_name, expected in (
        ("linux", "linux-cpu"),
        ("windows_portable", "windows-portable-cpu"),
        ("macos", "macos-cpu"),
    ):
        ctx = SimpleNamespace(lane_id=None, platform_name=platform_name)
        assert resolve_lane_id(ctx) == expected


def test_cpu_lane_honours_the_configured_timeout(monkeypatch):
    from comfy_test.orchestration.results import get_workflow_timeout
    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    assert get_workflow_timeout(120) == 120
    monkeypatch.setenv("COMFY_TEST_CUDA", "1")
    assert get_workflow_timeout(120) == 86400


def test_cpu_lane_resolves_against_the_cpu_index(monkeypatch):
    """Resolving against cu128 while installing from /whl/cpu is the bug."""
    from comfy_test.common.config import _index_variant
    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    assert _index_variant() == "cpu"


# Raw reads are allowed here: the helper itself, the env-var *setters*, and
# cdp_driver.py, which is shipped to the Desktop lane as standalone source text
# and cannot import the package.
_ALLOWED = {"common/accel.py", "platforms/desktop/cdp_driver.py"}
_READ = re.compile(r"""os\.environ\.get\(\s*["']COMFY_TEST_CUDA""")


def test_no_second_spelling_of_the_cuda_predicate():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWED:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _READ.search(line):
                offenders.append(f"{rel}:{n}")
    assert not offenders, (
        "Raw COMFY_TEST_CUDA reads outside common/accel.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `from comfy_test.common.accel import is_cuda_run`. The raw "
          "form is how the string \"0\" got treated as CUDA on every CPU lane."
    )
