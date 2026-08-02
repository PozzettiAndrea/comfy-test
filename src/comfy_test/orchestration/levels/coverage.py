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
        TestError: If any registered node is not referenced by any workflow,
            or any [test.coverage.inputs] declared value is not exercised
    """
    from ...comfyui.coverage import analyze_coverage

    ctx.log("\nAnalyzing workflow coverage...")
    input_coverage = getattr(ctx.config.coverage, "inputs", {}) if ctx.config else {}
    result = analyze_coverage(ctx.node_dir, input_coverage=input_coverage)

    for warning in result.warnings:
        ctx.log(f"  ! {warning}")

    if not result.registered:
        raise TestError(
            "Found 0 registered nodes -- this almost always means the static "
            "NODE_CLASS_MAPPINGS scan couldn't recognize this pack's "
            "registration pattern (e.g. a dict built from a function call, or "
            "an unsupported comprehension shape), not that the pack truly "
            "registers no nodes. Failing loudly instead of vacuously passing "
            "a meaningless 0/0 coverage check.",
            f"scanned: {result.pack_dir}",
        )

    ctx.log(
        f"Coverage: {len(result.tested)}/{len(result.registered)} registered nodes "
        f"used across {result.workflow_count} workflow(s) ({result.coverage_pct:.0f}%)"
    )
    if result.dispatched:
        ctx.log(
            f"  ({len(result.dispatched)} of those credited via dispatcher "
            "backend-map tracing, not a direct workflow reference)"
        )

    # Declared input-value coverage ([test.coverage.inputs])
    missing_inputs = result.untested_input_values
    for node_type, node_inputs in result.input_declared.items():
        for input_name, values in node_inputs.items():
            hits = result.input_hits.get(node_type, {}).get(input_name, {})
            covered = sum(1 for v in values if v in hits)
            ctx.log(
                f"Input coverage: {node_type}.{input_name} "
                f"{covered}/{len(values)} declared values"
            )

    problems = []
    if result.untested:
        problems.append(
            (
                f"{len(result.untested)} registered node(s) not used by any workflow",
                "\n".join(result.untested),
            )
        )
    if missing_inputs:
        n_missing = sum(len(vals) for _, _, vals in missing_inputs)
        problems.append(
            (
                f"{n_missing} declared input value(s) not exercised by any workflow",
                "\n".join(
                    f"{t}.{i} = {v!r}"
                    for t, i, vals in missing_inputs
                    for v in vals
                ),
            )
        )
    if problems:
        raise TestError(
            "; ".join(msg for msg, _ in problems),
            "\n".join(detail for _, detail in problems),
        )

    ok = "all registered nodes used by at least one workflow"
    if result.input_declared:
        ok += "; all declared input values exercised"
    ctx.log(f"Coverage check: OK ({ok})")
    return ctx
