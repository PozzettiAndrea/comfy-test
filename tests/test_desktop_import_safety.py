"""Nothing may import `cdp_driver` to reach a helper.

`platforms/desktop/cdp_driver.py` is not a module, it is a ~4,000-line script
shipped to the Desktop lane and executed in its own interpreter. Roughly 41% of
it is top-level statements, including a `_walk_first_run_wizard(...)` call with
a 1200-second timeout followed by `sys.exit(1)`, and a 1,337-line
`with sync_playwright()` block that is the entire desktop test.

`cli/_desktop_runner.py` used to import one helper from it while collecting
logs at the end of a run. That import ran the script: up to twenty minutes of
polling, then a `SystemExit` -- which is not an `Exception`, so the caller's
`except Exception` could not catch it, and the lane lost its logs artifact.

The helper now lives in `install_paths.py`, which is pure.
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "comfy_test"
_DRIVER = _SRC / "platforms" / "desktop" / "cdp_driver.py"

_IMPORTS_DRIVER = re.compile(
    r"(?:from\s+[\w.]*cdp_driver\s+import)|(?:import\s+[\w.]*\.cdp_driver\b)")


def test_nothing_imports_the_driver_script():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _DRIVER:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _IMPORTS_DRIVER.search(line):
                offenders.append(f"{path.relative_to(_SRC).as_posix()}:{n}: {line.strip()}")
    assert not offenders, (
        "cdp_driver.py runs the whole desktop test on import:\n  "
        + "\n  ".join(offenders)
        + "\n\nPut the helper in a pure module (see install_paths.py) instead."
    )


def test_install_paths_is_pure():
    """It must stay importable: no top-level work beyond defs and constants."""
    import ast
    tree = ast.parse((_SRC / "platforms" / "desktop" / "install_paths.py")
                     .read_text(encoding="utf-8"))
    for node in tree.body:
        assert isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                 ast.ClassDef, ast.Assign, ast.AnnAssign,
                                 ast.Expr)), f"top-level {type(node).__name__}"
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant), "top-level call/expression"


def test_the_helper_answers_without_desktop_installed():
    from comfy_test.platforms.desktop.install_paths import (
        find_active_comfy_install, installations_json_path)
    assert installations_json_path().name == "installations.json"
    try:
        find_active_comfy_install()
    except RuntimeError:
        pass  # the expected outcome on a machine with no Comfy Desktop
