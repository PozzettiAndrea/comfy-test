"""COVERAGE level - Static check that every registered node is used by a workflow."""

from ...common.errors import TestError
from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run COVERAGE level.

    Statically cross-references the pack's NODE_CLASS_MAPPINGS against the
    node types used in workflows/*.json. No server or node imports required --
    runs directly against ctx.node_dir, same as SYNTAX.

    Args:
        ctx: Level context

    Returns:
        Unchanged context (coverage doesn't add state)

    Raises:
        TestError: If any registered node is not referenced by any workflow
    """
    from ...comfyui.coverage import analyze_coverage

    ctx.log("\nAnalyzing workflow coverage...")
    result = analyze_coverage(ctx.node_dir)

    for warning in result.warnings:
        ctx.log(f"  ! {warning}")

    ctx.log(
        f"Coverage: {len(result.tested)}/{len(result.registered)} registered nodes "
        f"used across {result.workflow_count} workflow(s) ({result.coverage_pct:.0f}%)"
    )
    if result.dispatched:
        ctx.log(
            f"  ({len(result.dispatched)} of those credited via dispatcher "
            "backend-map tracing, not a direct workflow reference)"
        )

    if result.untested:
        raise TestError(
            f"{len(result.untested)} registered node(s) not used by any workflow",
            "\n".join(result.untested),
        )

    ctx.log("Coverage check: OK (all registered nodes used by at least one workflow)")
    return ctx
