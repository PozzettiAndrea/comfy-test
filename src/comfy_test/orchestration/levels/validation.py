"""VALIDATION level - Validate workflows via 3-level validation."""

import json
from pathlib import Path

from ...common.errors import TestError
from ...comfyui.validator import WorkflowValidation
from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run VALIDATION level.

    Runs 3-level workflow validation on all configured workflows:
    1. Schema - Widget values match allowed enums/types/ranges
    2. Graph - Connections are valid, all nodes exist
    3. Introspection - Node definitions are well-formed

    Args:
        ctx: Level context (must have api set)

    Returns:
        Unchanged context

    Raises:
        TestError: If any workflow fails validation
    """
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    workflows = ctx.config.workflow.workflows

    if not workflows:
        ctx.log("No workflows to validate")
        return ctx

    if not ctx.api:
        raise TestError("VALIDATION level requires API (run REGISTRATION first)")

    total_workflows = len(workflows)
    ctx.log(f"Validating {total_workflows} workflow(s)...")

    # Get object_info from server
    object_info = ctx.api.get_object_info()
    validator = WorkflowValidation(object_info)

    validation_errors = []
    for idx, workflow_file in enumerate(workflows, 1):
        workflow_path = _resolve_workflow_path(ctx, workflow_file)
        ctx.log(f"  [{idx}/{total_workflows}] Validating {workflow_file.name}")

        try:
            # Load workflow JSON
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)

            # Run validation
            result = validator.validate(workflow)

            if result.is_valid:
                ctx.log("    OK")
            else:
                error_msgs = [str(e) for e in result.errors]
                ctx.log(f"    FAILED: {len(result.errors)} error(s)")
                for err in result.errors[:5]:  # Show first 5
                    ctx.log(f"      {err}")
                if len(result.errors) > 5:
                    ctx.log(f"      ... and {len(result.errors) - 5} more")
                validation_errors.append((workflow_file.name, "; ".join(error_msgs)))

        except json.JSONDecodeError as e:
            ctx.log(f"    FAILED: Invalid JSON - {e}")
            validation_errors.append((workflow_file.name, f"Invalid JSON: {e}"))
        except FileNotFoundError:
            ctx.log(f"    FAILED: File not found")
            validation_errors.append((workflow_file.name, "File not found"))
        except Exception as e:
            ctx.log(f"    FAILED: {e}")
            validation_errors.append((workflow_file.name, str(e)))

    if validation_errors:
        raise TestError(
            f"Workflow validation failed ({len(validation_errors)} error(s))",
            "\n".join(f"  - {name}: {err}" for name, err in validation_errors)
        )

    return ctx


def _resolve_workflow_path(ctx: LevelContext, workflow_file: Path) -> Path:
    """Resolve workflow file path relative to node directory."""
    workflow_path = Path(workflow_file)
    if not workflow_path.is_absolute():
        workflow_path = ctx.node_dir / workflow_file
    return workflow_path
