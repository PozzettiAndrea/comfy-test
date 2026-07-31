"""CUSTOM level - run a node-supplied test hook.

Configured in the node's comfy-test.toml:

    [test]
    custom = "tests/my_check.py"

The hook is a Python file exposing `def run(ctx)` (or `def check(ctx)`). It gets
the full LevelContext -- the live ComfyUI `server`/`api`, `node_dir`, `paths`,
`registered_nodes`, `config`, `log` -- and reports the same way the built-in
levels do: raise to fail, return to pass. This is the escape hatch for
domain-specific assertions the 9 generic levels can't express (e.g. "my node
produced a valid STEP file", "the output isn't all-black").

Runs last (after execution), and depends on install + registration so the
server is live. No new trust boundary: comfy-test already runs the node's
install.py and executes its workflows.
"""

import importlib.util

from ...common.errors import TestError
from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run the node's custom hook, if one is configured."""
    script = getattr(ctx.config, "custom", None)
    if not script:
        ctx.log("No custom hook configured ([test] custom); skipping.")
        return ctx

    path = (ctx.node_dir / script).resolve()
    if not path.exists():
        raise TestError(
            f"Custom hook not found: {script}",
            f"[test] custom points at {path}, which does not exist.",
        )

    ctx.log(f"Running custom hook: {script}")
    spec = importlib.util.spec_from_file_location("comfy_test_custom_hook", path)
    if spec is None or spec.loader is None:
        raise TestError(f"Could not load custom hook: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise TestError(f"Custom hook failed to import: {script}", str(e))

    hook = getattr(module, "run", None) or getattr(module, "check", None)
    if not callable(hook):
        raise TestError(
            f"Custom hook {script} defines no run(ctx) or check(ctx) function",
            "Add e.g.  def run(ctx):  ...  (raise to fail, return to pass).",
        )

    try:
        result = hook(ctx)
    except TestError:
        raise
    except Exception as e:
        raise TestError(f"Custom hook failed: {script}", str(e))

    # The hook may return an updated context, or None (keep ours).
    return result if isinstance(result, LevelContext) else ctx
