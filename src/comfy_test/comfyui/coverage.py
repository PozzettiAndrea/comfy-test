"""Deterministic workflow coverage analysis for ComfyUI custom node packs.

Answers a single question without starting a server or importing node code:
*which registered nodes in this pack are not referenced by any workflow?*

Two static sources are cross-referenced:

1. **Registered nodes** -- the string keys of every ``NODE_CLASS_MAPPINGS``
   dict literal in the pack's Python source, collected via ``ast`` (no import,
   no execution). This mirrors how ComfyUI keys ``/object_info`` and how
   workflows reference node types.
2. **Workflow nodes** -- the ``type`` of every node in each ``workflows/*.json``
   file (litegraph format), including nodes nested inside subgraph definitions.

Because both sides are parsed statically, the result is reproducible: the same
files always yield the same coverage. Nodes whose mapping keys are *not* string
literals (built dynamically) can't be resolved statically and are surfaced as
warnings rather than silently dropped.
"""

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


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
    # node type -> sorted list of workflow filenames that reference it
    used: Dict[str, List[str]] = field(default_factory=dict)
    registered: Set[str] = field(default_factory=set)
    # workflow node types that are not registered by this pack (builtins / other packs)
    external: Set[str] = field(default_factory=set)
    # human-readable notes about things that couldn't be resolved statically
    warnings: List[str] = field(default_factory=list)
    workflow_count: int = 0

    @property
    def tested(self) -> List[str]:
        """Registered nodes referenced by at least one workflow."""
        return sorted(self.registered & set(self.used))

    @property
    def untested(self) -> List[str]:
        """Registered nodes referenced by NO workflow."""
        return sorted(self.registered - set(self.used))

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
                if any(_is_mapping_target(t) for t in node.targets) and isinstance(node.value, ast.Dict):
                    found |= _dict_literal_str_keys(node.value, rel, warnings)
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
                if node.target and _is_mapping_target(node.target) and isinstance(node.value, ast.Dict):
                    found |= _dict_literal_str_keys(node.value, rel, warnings)
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

    return found, warnings


# --------------------------------------------------------------------------
# Workflow-node discovery (JSON)
# --------------------------------------------------------------------------

def _collect_workflow_types(data: object) -> Set[str]:
    """Extract node ``type`` values from a parsed workflow (litegraph or API).

    Handles top-level ``nodes`` plus nodes nested in ``definitions.subgraphs``
    (litegraph subgraphs), and the API/prompt format ``{id: {class_type}}``.
    """
    types: Set[str] = set()

    def visit_nodes(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for n in nodes:
            if isinstance(n, dict):
                t = n.get("type")
                if isinstance(t, str):
                    types.add(t)

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
                    types.add(v["class_type"])

    return types


def discover_workflow_nodes(workflows_dir: Path) -> Tuple[Dict[str, List[str]], int, List[str]]:
    """Map each node type to the workflow files that reference it.

    Returns ``(used, workflow_count, warnings)`` where ``used`` is
    ``{node_type: [workflow_filename, ...]}``.
    """
    used: Dict[str, Set[str]] = {}
    warnings: List[str] = []
    count = 0

    if not workflows_dir.is_dir():
        return {}, 0, [f"no workflows/ directory at {workflows_dir}"]

    for wf in sorted(workflows_dir.glob("*.json")):
        count += 1
        try:
            data = json.loads(wf.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            warnings.append(f"{wf.name}: could not parse ({e.__class__.__name__})")
            continue
        for t in _collect_workflow_types(data):
            used.setdefault(t, set()).add(wf.name)

    return {t: sorted(files) for t, files in used.items()}, count, warnings


# --------------------------------------------------------------------------
# Top-level analysis
# --------------------------------------------------------------------------

def analyze_coverage(pack_dir: Path, workflows_dir: Path | None = None) -> CoverageResult:
    """Run a full deterministic coverage analysis for a node pack."""
    pack_dir = Path(pack_dir).resolve()
    workflows_dir = Path(workflows_dir).resolve() if workflows_dir else pack_dir / "workflows"

    registered, reg_warnings = discover_registered_nodes(pack_dir)
    used, wf_count, wf_warnings = discover_workflow_nodes(workflows_dir)

    return CoverageResult(
        pack_dir=pack_dir,
        workflows_dir=workflows_dir,
        used=used,
        registered=registered,
        external=set(used) - registered,
        warnings=reg_warnings + wf_warnings,
        workflow_count=wf_count,
    )
