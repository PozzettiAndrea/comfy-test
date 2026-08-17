"""The WARNINGS level reports antipatterns and never fails a build.

The "never fails" part is the load-bearing property: this level holds judgement
calls, and a gate that fails on a judgement call gets ignored, at which point it
stops catching the cases where it was right. So it is asserted here rather than
left to convention.
"""

import textwrap

from comfy_test.common.config import DEFAULT_LEVELS, LEVEL_REQUIRES, TestLevel
from comfy_test.orchestration.levels.warnings import (
    CHECKS,
    ROOT_ALLOWED,
    run,
)


class _Ctx:
    def __init__(self, node_dir):
        self.node_dir = node_dir
        self.lines = []

    def log(self, message="", *a, **k):
        self.lines.append(str(message))

    @property
    def text(self):
        return "\n".join(self.lines)


def test_level_is_opt_in_and_standalone():
    assert TestLevel.WARNINGS not in DEFAULT_LEVELS, "must not run by default"
    assert LEVEL_REQUIRES[TestLevel.WARNINGS] == [], "must need no env or server"


def test_never_raises_even_with_findings(tmp_path):
    (tmp_path / "stray_node.py").write_text("x = 1\n")
    (tmp_path / "model.safetensors").write_bytes(b"\0" * 16)
    (tmp_path / "cfg.py").write_text('P = "/home/someone/models/x.ckpt"\n')

    ctx = _Ctx(tmp_path)
    run(ctx)  # must not raise
    assert "stray_node.py" in ctx.text
    assert "model.safetensors" in ctx.text
    assert "hardcoded absolute path" in ctx.text


def test_clean_pack_reports_clean(tmp_path):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "nodes.py").write_text("import os\n")
    (tmp_path / "__init__.py").write_text("from .nodes import *\n")

    ctx = _Ctx(tmp_path)
    run(ctx)
    assert "clean" in ctx.text.lower()


def test_root_files_required_by_comfy_env_are_allowed(tmp_path):
    # serialization.py must sit at the pack root (comfy-env ADR-0015), so the
    # layout check must not flag it.
    for name in ROOT_ALLOWED:
        (tmp_path / name).write_text("x = 1\n")
    ctx = _Ctx(tmp_path)
    run(ctx)
    assert "pack root" not in ctx.text


def test_duplicate_detection_ignores_short_files(tmp_path):
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    short = "x = 1\n" * 10           # boilerplate, not a vendored copy
    (nodes / "a.py").write_text(short)
    (nodes / "b.py").write_text(short)
    ctx = _Ctx(tmp_path)
    run(ctx)
    assert "vendored more than once" not in ctx.text

    long = "x = 1\n" * 200
    (nodes / "c.py").write_text(long)
    (nodes / "d.py").write_text(long)
    ctx = _Ctx(tmp_path)
    run(ctx)
    assert "vendored more than once" in ctx.text


def test_a_broken_check_cannot_break_the_run(tmp_path, monkeypatch):
    def boom(node_dir):
        raise RuntimeError("check exploded")

    monkeypatch.setattr(
        "comfy_test.orchestration.levels.warnings.CHECKS",
        [("boom", "always explodes", boom)],
    )
    ctx = _Ctx(tmp_path)
    run(ctx)  # must still return
    assert "check itself failed" in ctx.text


def test_comments_are_not_flagged(tmp_path):
    (tmp_path / "x.py").write_text(
        textwrap.dedent('''\
            # see "/home/someone/notes.txt" for context
            # sys.path.append("somewhere")
            VALUE = 1
        ''')
    )
    ctx = _Ctx(tmp_path)
    run(ctx)
    assert "hardcoded absolute path" not in ctx.text
    assert "sys.path modification" not in ctx.text
