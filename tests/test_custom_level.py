"""Guard the pluggable CUSTOM test level (node-supplied hook)."""

import tempfile
from pathlib import Path

from comfy_test.common.config import TestLevel, ALL_LEVELS
from comfy_test.common.config_file import load_config
from comfy_test.orchestration.manager import LEVEL_RUNNERS, ALL_LEVELS as MGR_ALL
from comfy_test.orchestration.levels.custom import run as run_custom
from comfy_test.orchestration.context import LevelContext
from comfy_test.common.errors import TestError


def _node(toml: str, hook: str | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "comfy-test.toml").write_text(
        toml + '\n[test.platforms]\nplatforms = ["linux-cpu"]\n')
    if hook is not None:
        (d / "tests").mkdir(exist_ok=True)
        (d / "tests" / "hook.py").write_text(hook)
    return d


def _ctx(node: Path) -> LevelContext:
    return LevelContext(config=load_config(node / "comfy-test.toml"), node_dir=node,
                        platform_name="linux-cpu", log=lambda *a: None, output_base=node)


def test_manager_uses_single_source_all_levels():
    assert MGR_ALL is ALL_LEVELS  # the loose-end fix
    assert ALL_LEVELS[-1] == TestLevel.CUSTOM  # runs last
    assert TestLevel.CUSTOM in LEVEL_RUNNERS


def test_custom_hook_auto_enables_level():
    c = load_config(_node('[test]\ncustom = "tests/hook.py"', hook="def run(ctx): pass") / "comfy-test.toml")
    assert c.custom == "tests/hook.py"
    assert TestLevel.CUSTOM in c.levels
    # absent when not configured
    c2 = load_config(_node("[test]") / "comfy-test.toml")
    assert c2.custom is None and TestLevel.CUSTOM not in c2.levels


def test_custom_hook_pass_fail_missing():
    # pass: returns
    assert run_custom(_ctx(_node('[test]\ncustom="tests/hook.py"', hook="def run(ctx): return None"))) is not None
    # raise -> TestError
    try:
        run_custom(_ctx(_node('[test]\ncustom="tests/hook.py"', hook="def run(ctx): raise ValueError('x')")))
        assert False, "should have failed"
    except TestError:
        pass
    # missing file -> TestError
    try:
        run_custom(_ctx(_node('[test]\ncustom="tests/nope.py"')))
        assert False, "should have failed"
    except TestError:
        pass


if __name__ == "__main__":
    test_manager_uses_single_source_all_levels()
    test_custom_hook_auto_enables_level()
    test_custom_hook_pass_fail_missing()
    print("ok  custom level: single-source ALL_LEVELS, auto-enable, pass/fail/missing")
