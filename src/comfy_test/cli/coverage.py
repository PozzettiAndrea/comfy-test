"""Coverage command for comfy-test CLI.

Deterministically reports which registered nodes in a pack are not exercised by
any workflow in ``workflows/``. Static-only: no ComfyUI server, no node imports.
"""

import json as _json
import sys
from pathlib import Path

from ..comfyui.coverage import analyze_coverage


def _safe(s) -> str:
    """Sanitize for Windows cp1252 consoles (mirrors run.py)."""
    return str(s).encode("ascii", errors="replace").decode("ascii")


def cmd_coverage(args) -> int:
    """Report workflow coverage of a node pack's registered nodes."""
    pack_dir = Path(args.path).resolve()
    if not pack_dir.is_dir():
        print(f"[comfy-test] Not a directory: {pack_dir}", file=sys.stderr)
        return 2

    workflows_dir = Path(args.workflows).resolve() if args.workflows else None
    result = analyze_coverage(pack_dir, workflows_dir)

    if args.json:
        print(_json.dumps({
            "pack_dir": str(result.pack_dir),
            "workflows_dir": str(result.workflows_dir),
            "workflow_count": result.workflow_count,
            "registered_count": len(result.registered),
            "tested_count": len(result.tested),
            "untested_count": len(result.untested),
            "coverage_pct": round(result.coverage_pct, 1),
            "tested": result.tested,
            "untested": result.untested,
            "used": result.used,
            "dispatched": result.dispatched,
            "external": sorted(result.external),
            "warnings": result.warnings,
        }, indent=2))
        return 1 if (args.strict and result.untested) else 0

    if not result.registered:
        print(_safe(f"[comfy-test] No NODE_CLASS_MAPPINGS found under {result.pack_dir}"))
        print("            Is this a custom node pack directory?")
        return 2

    print(_safe(f"Node pack:  {result.pack_dir}"))
    print(_safe(f"Workflows:  {result.workflows_dir} ({result.workflow_count} file(s))"))
    print()
    print(_safe(
        f"Coverage:   {len(result.tested)}/{len(result.registered)} registered nodes "
        f"used in workflows ({result.coverage_pct:.0f}%)"
    ))
    if result.dispatched:
        print(_safe(
            f"            ({len(result.dispatched)} of those credited via dispatcher "
            "backend-map tracing, not a direct workflow reference -- see -v)"
        ))
    print()

    if result.untested:
        print(_safe(f"UNTESTED ({len(result.untested)}) -- registered but in no workflow:"))
        for name in result.untested:
            print(_safe(f"  - {name}"))
    else:
        print("All registered nodes are referenced by at least one workflow.")
    print()

    if args.verbose and result.tested:
        print(_safe(f"TESTED ({len(result.tested)}):"))
        for name in result.tested:
            direct = result.used.get(name)
            if direct:
                print(_safe(f"  + {name}  ->  {', '.join(direct)}"))
            else:
                via = ", ".join(result.dispatched.get(name, []))
                print(_safe(f"  + {name}  ->  {via}  [dispatched]"))
        print()

    if result.external:
        print(_safe(
            f"Note: {len(result.external)} workflow node type(s) are not registered by "
            "this pack (builtins or other packs):"
        ))
        for name in sorted(result.external):
            files = ", ".join(result.used.get(name, []))
            print(_safe(f"  ? {name}  ({files})"))
        print()

    if result.warnings:
        print(_safe(f"Warnings ({len(result.warnings)}):"))
        for w in result.warnings:
            print(_safe(f"  ! {w}"))
        print()

    return 1 if (args.strict and result.untested) else 0


def add_coverage_parser(subparsers):
    """Add the coverage subcommand parser."""
    parser = subparsers.add_parser(
        "coverage",
        help="Report which registered nodes are not used by any workflow",
        description=(
            "Statically cross-reference a pack's NODE_CLASS_MAPPINGS against the "
            "node types used in workflows/*.json. No server or imports required."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Node pack directory (default: current directory)",
    )
    parser.add_argument(
        "--workflows",
        default=None,
        metavar="DIR",
        help="Workflows directory (default: <path>/workflows)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also list tested nodes and the workflows that use them",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any registered node is untested (for CI)",
    )
    parser.set_defaults(func=cmd_coverage)
