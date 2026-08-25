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
from typing import Optional

from ...common.errors import TestError
from ..context import LevelContext


def _normalize_ns(name: str) -> str:
    """Lowercase a name and strip it to alphanumerics -> a single namespace
    token. 'GeometryPack' -> 'geometrypack', 'My Nodes!' -> 'mynodes'."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _display_namespace(pack_dir: Path) -> Optional[str]:
    """The pack's required JS namespace = its ComfyUI DisplayName, lowercased
    and stripped to alphanumerics (pyproject [tool.comfy].DisplayName). Falls
    back to [project].name minus a 'comfyui-' prefix (the globally-unique
    registry id). Returns None if pyproject declares no usable identity.

    This is the canonical, zero-config source: every registerExtension name in
    the pack's web dir must sit under this one prefix, so the pack's frontend
    can never squat another pack's names.
    """
    pyproject = pack_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        try:
            import tomllib as toml
        except ModuleNotFoundError:
            import tomli as toml
        data = toml.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None
    name = data.get("tool", {}).get("comfy", {}).get("DisplayName")
    if not name:
        name = data.get("project", {}).get("name", "") or ""
        for prefix in ("comfyui-", "comfyui_"):
            if name.lower().startswith(prefix):
                name = name[len(prefix):]
                break
    return _normalize_ns(name) or None


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


def _collect_node_ids(pack_dir: Path) -> set:
    """The pack's own ComfyUI node ids, so the foreign-node-hook rule can tell
    a widget hooking its OWN node from one squatting another pack's node.

    Reads both the V3 form (`node_id="X"` in define_schema) and the legacy
    `NODE_CLASS_MAPPINGS` dict keys / index assignments, across the pack's .py.
    """
    ids: set = set()
    for py in pack_dir.rglob("*.py"):
        try:
            txt = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids.update(re.findall(r"""node_id\s*=\s*["']([^"']+)["']""", txt))
        for block in re.findall(r"NODE_CLASS_MAPPINGS\s*(?::[^=\n]+)?=\s*\{([^}]*)\}", txt, flags=re.S):
            ids.update(re.findall(r"""["']([A-Za-z0-9_.\-]+)["']\s*:""", block))
        ids.update(re.findall(r"""NODE_CLASS_MAPPINGS\s*\[\s*["']([^"']+)["']\s*\]""", txt))
    return ids


def run(ctx: LevelContext) -> LevelContext:
    """Run the JAVASCRIPT collision lint. Raises TestError on any error finding."""
    from ...reporting.js_lint import lint_web_dir

    # Prefer the copy inside the materialized workspace; fall back to the source
    # tree. `ctx.paths` is None when no INSTALL level ran -- the standalone
    # `comfy-test lint` path -- and the source tree is the only copy there.
    pack_dir = ctx.node_dir
    if ctx.paths is not None:
        installed = ctx.paths.custom_nodes_dir / ctx.node_dir.name
        if installed.is_dir():
            pack_dir = installed

    web_dir = _resolve_web_dir(pack_dir)
    if web_dir is None:
        ctx.log("[javascript] pack ships no frontend web dir -- nothing to lint.")
        return ctx

    # ONE pack, ONE namespace, derived from the pack's published identity --
    # [tool.comfy] DisplayName, else [project] name. There is no config key: a
    # pack that ships several namespaces (usually vendored JS that kept its old
    # prefix) must rename its JS, not be grandfathered in. Amends ADR-0014,
    # which already kept declared namespaces only as an escape hatch.
    display_ns = _display_namespace(pack_dir)
    if not display_ns:
        raise TestError(
            "Pack has no published identity, so its JS namespace cannot be "
            "determined",
            "The javascript level requires one prefix that the pack provably "
            "owns, because ComfyUI loads every pack's JS into one shared "
            "browser page.\n\n"
            "Add either to pyproject.toml:\n"
            "    [project]\n"
            '    name = "comfyui-yourpack"\n'
            "  or:\n"
            "    [tool.comfy]\n"
            '    DisplayName = "YourPack"\n\n'
            "Both are required to publish to the Comfy Registry anyway. "
            "Nothing is added to comfy-test.toml.",
        )

    namespaces = [display_ns]
    ctx.log(f"[javascript] enforcing namespace '{display_ns}' -- every "
            f"registerExtension name, custom element, CSS selector and storage "
            f"key under the web dir must sit under '{display_ns}'.")

    node_ids = _collect_node_ids(pack_dir)
    ctx.log(f"[javascript] scanning {web_dir.relative_to(pack_dir)}/ "
            f"({len(list(web_dir.rglob('*.js')))} .js files) "
            f"namespaces={namespaces} node_ids={len(node_ids)}")
    findings = lint_web_dir(web_dir, namespaces, node_ids or None)
    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]

    # Sidecar for the dashboard (mirrors models.json).
    try:
        out = Path(ctx.output_base) / "javascript.json"
        out.write_text(json.dumps({
            "web_dir": str(web_dir.relative_to(pack_dir)),
            "namespaces": namespaces,
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
