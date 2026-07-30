"""Deterministic workflow coverage analysis for ComfyUI custom node packs.

Answers a single question without starting a server or importing node code:
*which registered nodes in this pack are not referenced by any workflow?*

Two static sources are cross-referenced:

1. **Registered nodes** -- the string keys of every ``NODE_CLASS_MAPPINGS``
   dict literal (or ``{cls.__name__: cls for cls in [...]}`` comprehension)
   in the pack's Python source, collected via ``ast`` (no import, no
   execution). This mirrors how ComfyUI keys ``/object_info`` and how
   workflows reference node types.
2. **Workflow nodes** -- the ``type`` of every node in each ``workflows/*.json``
   file (litegraph format), including nodes nested inside subgraph definitions.

Because both sides are parsed statically, the result is reproducible: the same
files always yield the same coverage. Nodes whose mapping keys are *not* string
literals (built dynamically) can't be resolved statically and are surfaced as
warnings rather than silently dropped.

**Dispatcher nodes (GraphBuilder expansion).** Some packs use ComfyUI's node
expansion API to fan a single visible node out to a hidden (``is_dev_only``)
backend node at execution time, based on a combo widget -- e.g. a "Remesh"
node with a "backend" dropdown that inserts a different registered node
depending on the selection. The saved workflow JSON only ever records the
dispatcher's own type, never the backend it resolved to, so a naive
type-string comparison always misreports those backend nodes as untested even
when a workflow demonstrably exercises them. By convention such a dispatcher
class exposes a class-level ``BACKEND_MAP = {"combo_value": "HiddenNodeId"}``
dict literal; this module statically finds those maps and, for each workflow
node of that dispatcher type, resolves its saved "backend" widget value
through the map to credit the real underlying node as tested.
"""

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# Directories never worth scanning for node registrations.
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "web", "assets", "docs",
    ".github", "workflows", ".ipynb_checkpoints", "_env", "venv", ".venv",
}


@dataclass
class CoverageResult:
    """Result of a coverage analysis for one node pack."""

    pack_dir: Path
    workflows_dir: Path
    # node type -> sorted list of workflow filenames that reference it directly
    used: Dict[str, List[str]] = field(default_factory=dict)
    # node type -> sorted list of human-readable dispatch provenance notes
    # (e.g. "remeshing_all.json (via GeomPackRemesh backend='geogram_anisotropic')")
    dispatched: Dict[str, List[str]] = field(default_factory=dict)
    registered: Set[str] = field(default_factory=set)
    # workflow node types that are not registered by this pack (builtins / other packs)
    external: Set[str] = field(default_factory=set)
    # human-readable notes about things that couldn't be resolved statically
    warnings: List[str] = field(default_factory=list)
    workflow_count: int = 0

    @property
    def tested(self) -> List[str]:
        """Registered nodes referenced by at least one workflow, directly or via dispatch."""
        return sorted(self.registered & (set(self.used) | set(self.dispatched)))

    @property
    def untested(self) -> List[str]:
        """Registered nodes referenced by NO workflow, directly or via dispatch."""
        return sorted(self.registered - (set(self.used) | set(self.dispatched)))

    @property
    def coverage_pct(self) -> float:
        if not self.registered:
            return 0.0
        return 100.0 * len(self.tested) / len(self.registered)


# --------------------------------------------------------------------------
# Registered-node discovery (AST, deterministic, no imports)
# --------------------------------------------------------------------------

def _dict_literal_str_keys(
    node: ast.Dict, rel: str, warnings: List[str]
) -> Set[str]:
    """Collect string-literal keys from a dict literal.

    ``**spread`` entries (key is None) are skipped -- they re-export another
    mapping whose own literal is scanned where it is defined. Non-literal keys
    (f-strings, names, calls) can't be resolved statically and are warned about.
    """
    keys: Set[str] = set()
    for key in node.keys:
        if key is None:  # dict unpacking: {**other}
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        else:
            warnings.append(
                f"{rel}:{getattr(key, 'lineno', '?')}: non-literal "
                f"NODE_CLASS_MAPPINGS key could not be resolved statically"
            )
    return keys


def _is_mapping_target(target: ast.expr) -> bool:
    """True if an assignment target is ``NODE_CLASS_MAPPINGS`` (name or attr)."""
    if isinstance(target, ast.Name):
        return target.id == "NODE_CLASS_MAPPINGS"
    if isinstance(target, ast.Attribute):
        return target.attr == "NODE_CLASS_MAPPINGS"
    return False


def _dict_comp_class_name_keys(
    node: ast.DictComp, rel: str, warnings: List[str]
) -> Set[str]:
    """Collect keys from a ``{cls.__name__: cls for cls in [A, B, ...]}`` comprehension.

    A common idiom for building ``NODE_CLASS_MAPPINGS`` from a list of already
    -imported node classes. ``cls.__name__`` can't be evaluated statically in
    general, but when the loop variable's ``__name__`` attribute is used as
    the key and the iterable is a literal list/tuple of bare names, each name
    IS that class's ``__name__`` at runtime (barring the vanishingly rare case
    of a class overriding ``__name__``), so the identifier text itself is a
    safe, deterministic stand-in.
    """
    keys: Set[str] = set()
    if len(node.generators) != 1:
        return keys
    gen = node.generators[0]
    if not isinstance(gen.target, ast.Name):
        return keys
    var = gen.target.id

    if not (
        isinstance(node.key, ast.Attribute)
        and node.key.attr == "__name__"
        and isinstance(node.key.value, ast.Name)
        and node.key.value.id == var
    ):
        return keys

    iterable = gen.iter
    if not isinstance(iterable, (ast.List, ast.Tuple)):
        return keys

    for elt in iterable.elts:
        if isinstance(elt, ast.Name):
            keys.add(elt.id)
        else:
            warnings.append(
                f"{rel}:{getattr(elt, 'lineno', '?')}: non-literal class reference in "
                f"NODE_CLASS_MAPPINGS dict-comprehension could not be resolved statically"
            )
    return keys


def discover_registered_nodes(pack_dir: Path) -> Tuple[Set[str], List[str]]:
    """Statically collect every registered node type name in a pack.

    Walks all ``*.py`` under ``pack_dir`` (minus build/output dirs) and gathers
    string-literal keys from, in each module:

    - ``NODE_CLASS_MAPPINGS = { "Type": Cls, ... }``
    - ``NODE_CLASS_MAPPINGS.update({ "Type": Cls, ... })``
    - ``NODE_CLASS_MAPPINGS["Type"] = Cls``

    Returns ``(node_type_names, warnings)``.
    """
    found: Set[str] = set()
    warnings: List[str] = []

    for py in sorted(pack_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.relative_to(pack_dir).parts):
            continue
        rel = str(py.relative_to(pack_dir))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError) as e:
            warnings.append(f"{rel}: could not parse ({e.__class__.__name__})")
            continue

        for node in ast.walk(tree):
            # NODE_CLASS_MAPPINGS = {...}   (and chained / annotated assigns)
            if isinstance(node, ast.Assign):
                if any(_is_mapping_target(t) for t in node.targets):
                    if isinstance(node.value, ast.Dict):
                        found |= _dict_literal_str_keys(node.value, rel, warnings)
                    elif isinstance(node.value, ast.DictComp):
                        found |= _dict_comp_class_name_keys(node.value, rel, warnings)
                # NODE_CLASS_MAPPINGS["Type"] = Cls
                for t in node.targets:
                    if (
                        isinstance(t, ast.Subscript)
                        and _is_mapping_target(t.value)
                        and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)
                    ):
                        found.add(t.slice.value)
            elif isinstance(node, ast.AnnAssign):
                if node.target and _is_mapping_target(node.target):
                    if isinstance(node.value, ast.Dict):
                        found |= _dict_literal_str_keys(node.value, rel, warnings)
                    elif isinstance(node.value, ast.DictComp):
                        found |= _dict_comp_class_name_keys(node.value, rel, warnings)
            # NODE_CLASS_MAPPINGS.update({...})
            elif isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "update"
                    and _is_mapping_target(fn.value)
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Dict):
                            found |= _dict_literal_str_keys(arg, rel, warnings)
                        elif isinstance(arg, ast.DictComp):
                            found |= _dict_comp_class_name_keys(arg, rel, warnings)

    return found, warnings


# --------------------------------------------------------------------------
# Dispatcher-node discovery (AST, deterministic, no imports)
# --------------------------------------------------------------------------

_DISPATCH_WIDGET_NAME = "backend"


def _dict_literal_str_pairs(
    node: ast.Dict, rel: str, warnings: List[str], label: str
) -> Dict[str, str]:
    """Collect string-literal key -> string-literal value pairs from a dict literal."""
    pairs: Dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        if key is None:  # dict unpacking: {**other}
            continue
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            warnings.append(
                f"{rel}:{getattr(key, 'lineno', '?')}: non-literal {label} key "
                f"could not be resolved statically"
            )
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            pairs[key.value] = value.value
        else:
            warnings.append(
                f"{rel}:{getattr(value, 'lineno', '?')}: non-literal {label} value "
                f"for {key.value!r} could not be resolved statically"
            )
    return pairs


def _find_schema_node_id(class_node: ast.ClassDef) -> Optional[str]:
    """Find node_id="..." passed to a ``*.Schema(...)`` call inside this class."""
    for node in ast.walk(class_node):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_schema_call = (
            (isinstance(fn, ast.Attribute) and fn.attr == "Schema")
            or (isinstance(fn, ast.Name) and fn.id == "Schema")
        )
        if not is_schema_call:
            continue
        for kw in node.keywords:
            if kw.arg == "node_id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
    return None


def discover_backend_maps(pack_dir: Path) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Statically collect dispatcher-node backend maps.

    For every class that defines both a ``*.Schema(node_id=...)`` call and a
    class-level ``BACKEND_MAP = {"combo_value": "HiddenNodeId", ...}`` dict
    literal, records ``{dispatcher_node_id: {combo_value: backend_node_id}}``.

    Returns ``(backend_maps, warnings)``.
    """
    backend_maps: Dict[str, Dict[str, str]] = {}
    warnings: List[str] = []

    for py in sorted(pack_dir.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in py.relative_to(pack_dir).parts):
            continue
        rel = str(py.relative_to(pack_dir))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            backend_map_dict = None
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "BACKEND_MAP" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Dict)
                ):
                    backend_map_dict = stmt.value
                    break
            if backend_map_dict is None:
                continue

            node_id = _find_schema_node_id(node)
            if node_id is None:
                warnings.append(
                    f"{rel}:{node.lineno}: class {node.name} has BACKEND_MAP but no "
                    f"resolvable node_id, skipping dispatch tracing for it"
                )
                continue

            pairs = _dict_literal_str_pairs(backend_map_dict, rel, warnings, "BACKEND_MAP")
            if pairs:
                backend_maps[node_id] = pairs

    return backend_maps, warnings


# --------------------------------------------------------------------------
# Workflow-node discovery (JSON)
# --------------------------------------------------------------------------

def _resolve_dispatch_selection(n: dict, widget_name: str = _DISPATCH_WIDGET_NAME) -> Optional[str]:
    """Extract the saved value of a named widget from a workflow node dict.

    Handles litegraph (UI) format -- where ``widgets_values`` is a list
    positionally aligned with the widget-bearing entries in ``inputs`` -- and
    the API/prompt format, where ``inputs`` already maps names to values.
    """
    if "class_type" in n:  # API / prompt format
        inputs = n.get("inputs")
        if isinstance(inputs, dict):
            v = inputs.get(widget_name)
            return v if isinstance(v, str) else None
        return None

    # litegraph (UI) format
    widgets_values = n.get("widgets_values")
    inputs = n.get("inputs")
    if isinstance(widgets_values, dict):
        v = widgets_values.get(widget_name)
        return v if isinstance(v, str) else None
    if isinstance(widgets_values, list) and isinstance(inputs, list):
        widget_inputs = [inp for inp in inputs if isinstance(inp, dict) and isinstance(inp.get("widget"), dict)]
        for idx, inp in enumerate(widget_inputs):
            if idx >= len(widgets_values):
                break
            name = (inp.get("widget") or {}).get("name") or inp.get("name")
            if name == widget_name:
                v = widgets_values[idx]
                return v if isinstance(v, str) else None
    return None


def _collect_workflow_types(
    data: object, backend_maps: Optional[Dict[str, Dict[str, str]]] = None
) -> Tuple[Set[str], List[Tuple[str, str, str]]]:
    """Extract node ``type`` values from a parsed workflow (litegraph or API),
    plus any GraphBuilder-dispatched backend nodes resolvable via backend_maps.

    Handles top-level ``nodes`` plus nodes nested in ``definitions.subgraphs``
    (litegraph subgraphs), and the API/prompt format ``{id: {class_type}}``.

    Returns ``(types, dispatched)`` where ``dispatched`` is a list of
    ``(backend_node_id, dispatcher_type, combo_value)`` tuples.
    """
    backend_maps = backend_maps or {}
    types: Set[str] = set()
    dispatched: List[Tuple[str, str, str]] = []

    def record(n: dict, t: str) -> None:
        types.add(t)
        bmap = backend_maps.get(t)
        if bmap:
            selection = _resolve_dispatch_selection(n)
            if selection is not None and selection in bmap:
                dispatched.append((bmap[selection], t, selection))

    def visit_nodes(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for n in nodes:
            if isinstance(n, dict):
                t = n.get("type")
                if isinstance(t, str):
                    record(n, t)

    if isinstance(data, dict):
        if "nodes" in data:  # litegraph (UI) format
            visit_nodes(data.get("nodes"))
            if isinstance(data.get("definitions"), dict):
                for sg in data["definitions"].get("subgraphs", []) or []:
                    if isinstance(sg, dict):
                        visit_nodes(sg.get("nodes"))
        else:  # API / prompt format: {node_id: {"class_type": ...}}
            for v in data.values():
                if isinstance(v, dict) and isinstance(v.get("class_type"), str):
                    record(v, v["class_type"])

    return types, dispatched


def discover_workflow_nodes(
    workflows_dir: Path, backend_maps: Optional[Dict[str, Dict[str, str]]] = None
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], int, List[str]]:
    """Map each node type to the workflow files that reference it, plus any
    dispatcher-resolved backend nodes.

    Returns ``(used, dispatched, workflow_count, warnings)`` where ``used`` is
    ``{node_type: [workflow_filename, ...]}`` and ``dispatched`` is
    ``{backend_node_id: [provenance_note, ...]}``.
    """
    used: Dict[str, Set[str]] = {}
    dispatched: Dict[str, Set[str]] = {}
    warnings: List[str] = []
    count = 0

    if not workflows_dir.is_dir():
        return {}, {}, 0, [f"no workflows/ directory at {workflows_dir}"]

    for wf in sorted(workflows_dir.glob("*.json")):
        count += 1
        try:
            data = json.loads(wf.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            warnings.append(f"{wf.name}: could not parse ({e.__class__.__name__})")
            continue
        types, dispatch_records = _collect_workflow_types(data, backend_maps)
        for t in types:
            used.setdefault(t, set()).add(wf.name)
        for backend_id, dispatcher_type, selection in dispatch_records:
            note = f"{wf.name} (via {dispatcher_type} backend={selection!r})"
            dispatched.setdefault(backend_id, set()).add(note)

    return (
        {t: sorted(files) for t, files in used.items()},
        {t: sorted(notes) for t, notes in dispatched.items()},
        count,
        warnings,
    )


# --------------------------------------------------------------------------
# Top-level analysis
# --------------------------------------------------------------------------

def analyze_coverage(pack_dir: Path, workflows_dir: Path | None = None) -> CoverageResult:
    """Run a full deterministic coverage analysis for a node pack."""
    pack_dir = Path(pack_dir).resolve()
    workflows_dir = Path(workflows_dir).resolve() if workflows_dir else pack_dir / "workflows"

    registered, reg_warnings = discover_registered_nodes(pack_dir)
    backend_maps, bmap_warnings = discover_backend_maps(pack_dir)
    used, dispatched, wf_count, wf_warnings = discover_workflow_nodes(workflows_dir, backend_maps)

    return CoverageResult(
        pack_dir=pack_dir,
        workflows_dir=workflows_dir,
        used=used,
        dispatched=dispatched,
        registered=registered,
        external=set(used) - registered,
        warnings=reg_warnings + bmap_warnings + wf_warnings,
        workflow_count=wf_count,
    )
