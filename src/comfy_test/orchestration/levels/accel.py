"""ACCEL level - accelerator packages must be imported lazily.

The rule: a package named in an env's ``[cuda] packages`` must be imported
INSIDE the node that declares ``ACCELERATOR``, never at module top level. A
top-level import kills comfy-env's metadata scan on any machine where the
package is absent -- which silently unregisters EVERY node in that env, not
just the one that needed it.

comfy-env observes this at runtime by diffing ``sys.modules`` after the scan,
which is authoritative but only tells you once the env is built and the pack
is installed. This level answers the same question on a bare checkout, before
anything ships, which is the part CI needs.

Name resolution is the hard part, and the reason this is not a naive grep. A
distribution name is not an import name: ``faithc-aot`` installs
``faithcontour``, ``pyyaml`` installs ``yaml``. Guessing by
``name.replace("-", "_")`` is wrong for both and, worse, wrong SILENTLY -- the
import simply never matches and the file passes. So:

* if comfy-env has installed the env, ``env.stamp.json`` carries the real
  mapping (``accel_imports``), recorded at install time when the env existed
  and its metadata was readable. That is exact.
* otherwise the package is reported as UNRESOLVED and counted as a warning.
  Not as a pass -- "I could not check this" and "this is fine" are different
  answers, and conflating them is how the old comfy-env-side lint managed to
  approve a top-level ``import faithcontour``.
"""

import ast
import json
from pathlib import Path
from typing import Dict, List, Set

from ...common.errors import TestError
from ..context import LevelContext

_SIDECAR = "accel.json"


def _normalize(dist: str) -> str:
    return str(dist).strip().lower().replace("_", "-")


def _stamped_imports() -> Dict[str, List[str]]:
    """Union every materialized env's recorded dist -> import mapping.

    A union is correct because the mapping is a property of the distribution,
    not of the env that happens to hold it. Scanning the workspace avoids
    reproducing comfy-env's env-naming rules here.
    """
    try:
        from comfy_env.environment.cache import get_workspace_dir
        root = Path(get_workspace_dir())
    except Exception:
        import os
        override = os.environ.get("COMFY_ENV_ROOT")
        if not override:
            return {}
        root = Path(override)

    merged: Dict[str, List[str]] = {}
    for stamp in sorted(root.glob("envs/*/env.stamp.json")):
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for dist, imports in (data.get("accel_imports") or {}).items():
            merged.setdefault(_normalize(dist), [])
            for imp in imports:
                if imp not in merged[_normalize(dist)]:
                    merged[_normalize(dist)].append(imp)
    return merged


def _top_level_imports(tree: ast.Module):
    """Yield (stmt, guarded) for module-body imports; guarded = inside try."""
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            yield stmt, False
        elif isinstance(stmt, ast.Try):
            for inner in stmt.body:
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    yield inner, True


def _imported_roots(stmt) -> List[str]:
    if isinstance(stmt, ast.Import):
        return [alias.name.split(".")[0].lower() for alias in stmt.names]
    if isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.level == 0:
        return [stmt.module.split(".")[0].lower()]
    return []


def _module_declares_accelerator(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                targets = []
                if isinstance(stmt, ast.Assign):
                    targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    targets = [stmt.target.id]
                if "ACCELERATOR" in targets:
                    return True
    return False


def _uses_torch_cuda(tree: ast.Module) -> int:
    """Line of first torch.cuda attribute access, or 0."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "cuda"
                and isinstance(node.value, ast.Name) and node.value.id == "torch"):
            return node.lineno
    return 0


def _declared_packages(config_path: Path) -> List[str]:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    packages = (data.get("cuda") or {}).get("packages") or []
    if isinstance(packages, str):
        packages = [packages]
    return [str(p) for p in packages]


def lint_accelerator_imports(root: Path) -> List[Dict]:
    """Lint every comfy-env.toml-scoped env under root. Returns findings."""
    findings: List[Dict] = []
    root = Path(root)
    stamped = _stamped_imports()

    for config_path in sorted(root.rglob("comfy-env.toml")):
        declared = _declared_packages(config_path)
        if not declared:
            continue

        accel_names: Set[str] = set()
        unresolved: List[str] = []
        for pkg in declared:
            key = _normalize(pkg)
            if key in stamped:
                accel_names.update(n.lower() for n in stamped[key])
            else:
                # Record the guess so an obvious case is still caught, but
                # report that it is a guess.
                accel_names.add(key.replace("-", "_"))
                unresolved.append(pkg)

        if unresolved:
            findings.append({
                "level": "warning",
                "file": str(config_path.relative_to(root)),
                "line": 0,
                "message": (
                    f"import name(s) for {', '.join(unresolved)} are not "
                    f"recorded in any env.stamp.json, so this check fell back "
                    f"to guessing <name with - as _>. Build the env "
                    f"(`comfy-env install --dir <pack>`) for an exact check; "
                    f"comfy-env's scan-time check covers it either way."),
            })

        for py in sorted(config_path.parent.rglob("*.py")):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            rel = py.relative_to(root)

            for stmt, guarded in _top_level_imports(tree):
                hit = [r for r in _imported_roots(stmt) if r in accel_names]
                if not hit:
                    continue
                if guarded:
                    findings.append({
                        "level": "warning", "file": str(rel), "line": stmt.lineno,
                        "message": (f"top-level import of accelerator package "
                                    f"{hit[0]} (guarded by try/except -- survives "
                                    f"the scan, but the rule is lazy imports "
                                    f"inside the declaring node)")})
                else:
                    findings.append({
                        "level": "error", "file": str(rel), "line": stmt.lineno,
                        "message": (f"top-level import of accelerator package "
                                    f"{hit[0]} -- fatal to the metadata scan on "
                                    f"machines without it; import lazily inside "
                                    f"the ACCELERATOR-declaring node")})

            cuda_line = _uses_torch_cuda(tree)
            if cuda_line and not _module_declares_accelerator(tree):
                findings.append({
                    "level": "warning", "file": str(rel), "line": cuda_line,
                    "message": ("torch.cuda used but no class in this module "
                                "declares ACCELERATOR -- fine if there is a real "
                                "CPU fallback (opportunistic GPU), otherwise "
                                "declare ACCELERATOR")})
    return findings


def run(ctx: LevelContext) -> LevelContext:
    """Run ACCEL level checks.

    Raises:
        TestError: if any error-level finding is present.
    """
    findings = lint_accelerator_imports(ctx.node_dir)
    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warning"]

    if not findings:
        ctx.log("[accel] no accelerator packages declared, or all imports lazy")
    for f in findings:
        where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        ctx.log(f"[accel] {f['level'].upper()} {where}: {f['message']}")
    ctx.log(f"[accel] {len(errors)} error(s), {len(warnings)} warning(s)")

    try:
        (Path(ctx.output_base) / _SIDECAR).write_text(
            json.dumps({
                "findings": findings,
                "summary": {"errors": len(errors), "warnings": len(warnings)},
            }, indent=2), encoding="utf-8")
    except OSError as e:
        ctx.log(f"[accel] could not write {_SIDECAR}: {e}")

    if errors:
        raise TestError(
            f"{len(errors)} accelerator package(s) imported at module top level",
            details="\n".join(
                f"{f['file']}:{f['line']}: {f['message']}" for f in errors),
        )
    return ctx
