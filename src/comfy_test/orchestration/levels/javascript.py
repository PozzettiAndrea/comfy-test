"""JAVASCRIPT level - static collision lint of the pack's frontend JS.

Scans the pack's served web dir for shared-realm collision hazards (window.*
writes that leak into the main page, un-namespaced registerExtension names,
unguarded message listeners, shared-object monkeypatches). See reporting/
js_lint.py for the rules. Errors fail the level; warnings are advisory and
written to a javascript.json sidecar for the dashboard.
"""

import json
import re
from pathlib import Path
from typing import List, Optional

from ...common.errors import TestError
from ..context import LevelContext


def _guess_namespace(pack_name: str) -> str:
    """A best-effort namespace prefix from the pack folder name."""
    n = pack_name.lower()
    for prefix in ("comfyui-", "comfyui_"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    return re.sub(r"[^a-z0-9]+", "", n) or "pack"


def _resolve_web_dir(pack_dir: Path) -> Optional[Path]:
    """Find the pack's served frontend dir: pyproject [tool.comfy].web first,
    then the WEB_DIRECTORY attribute in __init__.py, then common names."""
    pyproject = pack_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            try:
                import tomllib as toml
            except ModuleNotFoundError:
                import tomli as toml
            data = toml.loads(pyproject.read_text(encoding="utf-8"))
            web = data.get("tool", {}).get("comfy", {}).get("web")
            if web:
                cand = (pack_dir / web).resolve()
                if cand.is_dir():
                    return cand
        except Exception:
            pass
    init = pack_dir / "__init__.py"
    if init.is_file():
        m = re.search(r"""WEB_DIRECTORY\s*=\s*["']([^"']+)["']""",
                      init.read_text(encoding="utf-8", errors="replace"))
        if m:
            cand = (pack_dir / m.group(1)).resolve()
            if cand.is_dir():
                return cand
    for name in ("web", "javascript"):
        cand = pack_dir / name
        if cand.is_dir():
            return cand
    return None


def run(ctx: LevelContext) -> LevelContext:
    """Run the JAVASCRIPT collision lint. Raises TestError on any error finding."""
    from ...reporting.js_lint import lint_web_dir

    pack_dir = ctx.paths.custom_nodes_dir / ctx.node_dir.name
    if not pack_dir.is_dir():
        pack_dir = ctx.node_dir  # dev / vendored fallback

    web_dir = _resolve_web_dir(pack_dir)
    if web_dir is None:
        ctx.log("[javascript] pack ships no frontend web dir -- nothing to lint.")
        return ctx

    declared = list(ctx.config.javascript.namespaces)
    if declared:
        namespaces, declared_flag = declared, True
    else:
        namespaces, declared_flag = [_guess_namespace(ctx.node_dir.name)], False
        ctx.log(f"[javascript] no [test.javascript] namespaces declared; "
                f"guessing '{namespaces[0]}' and treating name rules as advisory. "
                f"Declare namespaces to enforce them.")

    ctx.log(f"[javascript] scanning {web_dir.relative_to(pack_dir)}/ "
            f"({len(list(web_dir.rglob('*.js')))} .js files) "
            f"namespaces={namespaces}")
    findings = lint_web_dir(web_dir, namespaces, declared_flag)
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]

    # Sidecar for the dashboard (mirrors models.json).
    try:
        out = Path(ctx.output_base) / "javascript.json"
        out.write_text(json.dumps({
            "web_dir": str(web_dir.relative_to(pack_dir)),
            "namespaces": namespaces,
            "namespaces_declared": declared_flag,
            "summary": {"errors": len(errors), "warnings": len(warns)},
            "findings": [f.as_dict() for f in findings],
        }, indent=2), encoding="utf-8")
    except Exception as e:
        ctx.log(f"[javascript] could not write javascript.json: {e}")

    for f in findings:
        ctx.log(f"  [{f.level.upper()}] {f.file}:{f.line} {f.rule} -- {f.message}")

    ctx.log(f"[javascript] {len(errors)} error(s), {len(warns)} warning(s)")
    if errors:
        raise TestError(
            f"{len(errors)} JavaScript collision error(s) -- this pack's frontend "
            f"can break other packs in the shared browser page.",
            "; ".join(f"{f.file}:{f.line} {f.rule}" for f in errors[:5]),
        )
    return ctx
