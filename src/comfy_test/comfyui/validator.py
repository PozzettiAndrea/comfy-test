"""Workflow validation for ComfyUI workflows."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ValidationError:
    """A single validation error."""
    node_id: int
    node_type: str
    message: str
    level: str  # "schema", "graph", "introspection"

    def __str__(self) -> str:
        return f"[{self.level}] Node {self.node_id} ({self.node_type}): {self.message}"


@dataclass
class ValidationResult:
    """Result of workflow validation."""
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


class WorkflowValidation:
    """Validates ComfyUI workflows against node schemas.

    Implements 3 levels of validation:
    - Level 1: Schema validation (widget values match allowed types/enums/ranges)
    - Level 2: Graph validation (connections are valid, all nodes exist)
    - Level 3: Node introspection (INPUT_TYPES, RETURN_TYPES are valid)
    """

    def __init__(self, object_info: Dict[str, Any]):
        """
        Args:
            object_info: Node definitions from /object_info API
        """
        self.object_info = object_info

    def validate(self, workflow: Dict[str, Any]) -> ValidationResult:
        """Run all validation levels on a workflow.

        Args:
            workflow: Parsed workflow JSON

        Returns:
            ValidationResult with errors and warnings
        """
        result = ValidationResult()

        # Level 1: Schema validation
        result.errors.extend(self._validate_schema(workflow))

        # Level 2: Graph validation
        result.errors.extend(self._validate_graph(workflow))

        # Level 3: Node introspection
        result.errors.extend(self._validate_introspection(workflow))

        return result

    def validate_file(self, workflow_path: Path) -> ValidationResult:
        """Validate a workflow JSON file.

        Args:
            workflow_path: Path to workflow JSON file

        Returns:
            ValidationResult
        """
        workflow_path = Path(workflow_path)
        with open(workflow_path, encoding='utf-8-sig') as f:
            workflow = json.load(f)
        return self.validate(workflow)

    def _validate_schema(self, workflow: Dict[str, Any]) -> List[ValidationError]:
        """Level 1: Validate widget values against node schemas."""
        errors = []

        for node in workflow.get("nodes", []):
            node_id = node.get("id", 0)
            node_type = node.get("type", "unknown")

            # Check node type exists
            if node_type not in self.object_info:
                errors.append(ValidationError(
                    node_id, node_type,
                    f"Unknown node type: {node_type}",
                    "schema"
                ))
                continue

            schema = self.object_info[node_type]
            errors.extend(self._validate_widgets(node, schema))

        return errors

    # Widget types that are uppercase but NOT connection types
    WIDGET_TYPES = {"BOOLEAN", "INT", "FLOAT", "STRING"}

    def _validate_widgets(
        self,
        node: Dict[str, Any],
        schema: Dict[str, Any]
    ) -> List[ValidationError]:
        """Validate widget values for a single node."""
        errors = []
        node_id = node.get("id", 0)
        node_type = node.get("type", "unknown")

        inputs = schema.get("input", {})
        required = inputs.get("required", {})
        optional = inputs.get("optional", {})
        all_inputs = {**required, **optional}

        widgets_values = node.get("widgets_values", [])
        widget_idx = 0

        for input_name, input_spec in all_inputs.items():
            if not isinstance(input_spec, (list, tuple)) or len(input_spec) < 1:
                continue

            input_type = input_spec[0]

            # Skip connection types (uppercase like IMAGE, MASK, etc.)
            # But keep widget types (BOOLEAN, INT, FLOAT, STRING) which are also uppercase
            if isinstance(input_type, str) and input_type.isupper() and input_type not in self.WIDGET_TYPES:
                continue

            # This is a widget - get its value
            if widget_idx >= len(widgets_values):
                # No more widget values - might be using defaults
                break

            value = widgets_values[widget_idx]
            widget_idx += 1

            # Validate based on input type
            error = self._validate_value(input_name, input_type, input_spec, value)
            if error:
                errors.append(ValidationError(node_id, node_type, error, "schema"))

        return errors

    def _validate_value(
        self,
        input_name: str,
        input_type: Any,
        input_spec: List[Any],
        value: Any
    ) -> Optional[str]:
        """Validate a single widget value against its spec.

        Returns error message if invalid, None if valid.
        """
        # Get options dict (second element of spec, if present)
        opts = input_spec[1] if len(input_spec) > 1 and isinstance(input_spec[1], dict) else {}

        # Enum validation - input_type is a list of allowed values
        if isinstance(input_type, list):
            # Skip validation for file-based inputs (dynamic content)
            if opts.get("image_upload") or opts.get("file_upload"):
                return None
            if value not in input_type:
                return f"'{input_name}': '{value}' not in allowed values {input_type}"
            return None

        # INT validation
        if input_type == "INT":
            if not isinstance(value, (int, float)):
                return f"'{input_name}': expected INT, got {type(value).__name__}"
            min_val = opts.get("min")
            max_val = opts.get("max")
            if min_val is not None and value < min_val:
                return f"'{input_name}': {value} < minimum {min_val}"
            if max_val is not None and value > max_val:
                return f"'{input_name}': {value} > maximum {max_val}"

        # FLOAT validation
        elif input_type == "FLOAT":
            if not isinstance(value, (int, float)):
                return f"'{input_name}': expected FLOAT, got {type(value).__name__}"
            min_val = opts.get("min")
            max_val = opts.get("max")
            if min_val is not None and value < min_val:
                return f"'{input_name}': {value} < minimum {min_val}"
            if max_val is not None and value > max_val:
                return f"'{input_name}': {value} > maximum {max_val}"

        # STRING validation
        elif input_type == "STRING":
            if not isinstance(value, str):
                return f"'{input_name}': expected STRING, got {type(value).__name__}"

        # BOOLEAN validation
        elif input_type == "BOOLEAN":
            if not isinstance(value, bool):
                return f"'{input_name}': expected BOOLEAN, got {type(value).__name__}"

        return None

    def _validate_graph(self, workflow: Dict[str, Any]) -> List[ValidationError]:
        """Level 2: Validate graph connections."""
        errors = []

        nodes = workflow.get("nodes", [])
        links = workflow.get("links", [])

        # Build node lookup
        nodes_by_id = {n.get("id"): n for n in nodes}

        # Validate each link
        for link in links:
            if not isinstance(link, list) or len(link) < 6:
                continue

            link_id, from_node, from_slot, to_node, to_slot, link_type = link[:6]

            # Check source node exists
            if from_node not in nodes_by_id:
                errors.append(ValidationError(
                    from_node, "unknown",
                    f"Link {link_id}: source node {from_node} does not exist",
                    "graph"
                ))
                continue

            # Check target node exists
            if to_node not in nodes_by_id:
                errors.append(ValidationError(
                    to_node, "unknown",
                    f"Link {link_id}: target node {to_node} does not exist",
                    "graph"
                ))
                continue

            # Validate connection types match
            from_node_obj = nodes_by_id[from_node]
            to_node_obj = nodes_by_id[to_node]
            from_type = from_node_obj.get("type", "unknown")
            to_type = to_node_obj.get("type", "unknown")

            # Check output type matches expected input type
            if from_type in self.object_info and to_type in self.object_info:
                error = self._validate_connection(
                    from_node_obj, from_slot, to_node_obj, to_slot, link_type
                )
                if error:
                    errors.append(ValidationError(
                        to_node, to_type, error, "graph"
                    ))

        return errors

    def _validate_connection(
        self,
        from_node: Dict[str, Any],
        from_slot: int,
        to_node: Dict[str, Any],
        to_slot: int,
        declared_type: str
    ) -> Optional[str]:
        """Validate a single connection between nodes.

        Returns error message if invalid, None if valid.
        """
        from_type = from_node.get("type", "unknown")
        to_type = to_node.get("type", "unknown")

        # Get output type from source node schema
        from_schema = self.object_info.get(from_type, {})
        from_outputs = from_schema.get("output", [])

        if from_slot >= len(from_outputs):
            return f"Output slot {from_slot} does not exist on {from_type}"

        output_type = from_outputs[from_slot]

        # Get input type from target node's inputs array in the workflow
        # This is more reliable than inferring from schema because:
        # 1. The workflow explicitly stores connection slot types
        # 2. Some inputs (like STRING) can be both widgets AND connections
        to_inputs = to_node.get("inputs", [])

        if to_slot >= len(to_inputs):
            return f"Input slot {to_slot} does not exist on {to_type}"

        target_input = to_inputs[to_slot]
        target_input_type = target_input.get("type", "unknown")

        # Check type compatibility
        # ComfyUI allows some type coercion:
        # - "*" (any type) matches everything
        # - Exact type match
        # - Union types: "STRING,FILE_3D_GLB,FILE_3D_FBX" means any of those types
        if output_type == "*" or target_input_type == "*":
            return None

        # Handle union types (comma-separated list of accepted types)
        accepted_types = [t.strip() for t in target_input_type.split(",")]
        if output_type not in accepted_types:
            return f"Type mismatch: {from_type} outputs {output_type}, but {to_type} expects {target_input_type}"

        return None

    def _validate_introspection(self, workflow: Dict[str, Any]) -> List[ValidationError]:
        """Level 3: Validate node introspection data from object_info.

        Checks that each node in the workflow has valid:
        - input: dict with required/optional structure
        - output: list of output types
        - output_name: list of output names (matching output length)
        - name: internal function name
        """
        errors = []

        for node in workflow.get("nodes", []):
            node_id = node.get("id", 0)
            node_type = node.get("type", "unknown")

            if node_type not in self.object_info:
                # Already caught in Level 1
                continue

            schema = self.object_info[node_type]

            # Check input structure
            inputs = schema.get("input", {})
            if not isinstance(inputs, dict):
                errors.append(ValidationError(
                    node_id, node_type,
                    f"INPUT_TYPES returned invalid type: {type(inputs).__name__}",
                    "introspection"
                ))
                continue

            # Check required inputs have valid structure
            required = inputs.get("required", {})
            if required and not isinstance(required, dict):
                errors.append(ValidationError(
                    node_id, node_type,
                    f"INPUT_TYPES 'required' is not a dict",
                    "introspection"
                ))

            # Check optional inputs have valid structure
            optional = inputs.get("optional", {})
            if optional and not isinstance(optional, dict):
                errors.append(ValidationError(
                    node_id, node_type,
                    f"INPUT_TYPES 'optional' is not a dict",
                    "introspection"
                ))

            # Check output types
            outputs = schema.get("output", [])
            output_names = schema.get("output_name", [])

            if not isinstance(outputs, list):
                errors.append(ValidationError(
                    node_id, node_type,
                    f"RETURN_TYPES is not a list: {type(outputs).__name__}",
                    "introspection"
                ))
            elif output_names and len(outputs) != len(output_names):
                errors.append(ValidationError(
                    node_id, node_type,
                    f"RETURN_TYPES ({len(outputs)}) doesn't match RETURN_NAMES ({len(output_names)})",
                    "introspection"
                ))

            # Check function name exists
            func_name = schema.get("name")
            if not func_name:
                errors.append(ValidationError(
                    node_id, node_type,
                    f"Node has no FUNCTION defined",
                    "introspection"
                ))

        return errors
