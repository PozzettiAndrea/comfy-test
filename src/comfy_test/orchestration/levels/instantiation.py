"""INSTANTIATION level - Test node constructors."""

import json
import subprocess

from ...common.errors import TestError
from ..context import LevelContext


# Script template for testing node instantiation in subprocess
INSTANTIATION_SCRIPT = '''
import sys
import os
import json
from pathlib import Path

# Disable CUDA to prevent crashes on CPU-only machines
# (model_management.py calls torch.cuda at import time)
os.environ["CUDA_VISIBLE_DEVICES"] = ""

# Mock CUDA packages if needed
cuda_packages = {cuda_packages_json}
for pkg in cuda_packages:
    if pkg not in sys.modules:
        import types
        import importlib.machinery
        mock_module = types.ModuleType(pkg)
        mock_module.__spec__ = importlib.machinery.ModuleSpec(pkg, None)
        sys.modules[pkg] = mock_module

# Import ComfyUI's folder_paths to set up paths
import folder_paths

# Add custom_nodes directory to sys.path for proper package imports
custom_nodes_dir = Path("{custom_nodes_dir}")
if str(custom_nodes_dir) not in sys.path:
    sys.path.insert(0, str(custom_nodes_dir))

# Import the node as a proper package
node_name = "{node_name}"
try:
    import importlib
    module = importlib.import_module(node_name)
except ImportError as e:
    print(json.dumps({{"success": False, "error": f"Failed to import {{node_name}}: {{e}}"}}))
    sys.exit(1)

# Get NODE_CLASS_MAPPINGS
mappings = getattr(module, "NODE_CLASS_MAPPINGS", {{}})

errors = []
instantiated = []

for name, cls in mappings.items():
    print(f"Instantiating: {{name}}", flush=True)
    try:
        instance = cls()
        instantiated.append(name)
        print(f"  OK: {{name}}", flush=True)
    except Exception as e:
        print(f"  FAILED: {{name}} - {{e}}", flush=True)
        errors.append({{"node": name, "error": str(e)}})

result = {{
    "success": len(errors) == 0,
    "instantiated": instantiated,
    "errors": errors,
}}
print(json.dumps(result))
'''


def run(ctx: LevelContext) -> LevelContext:
    """Run INSTANTIATION level.

    Tests that all node constructors can be called without errors by running
    a subprocess that imports NODE_CLASS_MAPPINGS and calls each constructor.

    Args:
        ctx: Level context (must have paths, cuda_packages set)

    Returns:
        Unchanged context

    Raises:
        TestError: If any node fails to instantiate
    """
    ctx.log("Testing node constructors...")

    # Build the test script
    script = INSTANTIATION_SCRIPT.format(
        custom_nodes_dir=str(ctx.paths.custom_nodes_dir).replace("\\", "/"),
        node_name=ctx.node_dir.name,
        cuda_packages_json=json.dumps(list(ctx.cuda_packages)),
    )

    # Run the script
    result = subprocess.run(
        [str(ctx.paths.python), "-c", script],
        cwd=str(ctx.paths.comfyui_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise TestError(
            "Instantiation test failed",
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    # Parse result
    try:
        stdout = result.stdout.strip()
        json_line = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                json_line = line
        if json_line is None:
            raise json.JSONDecodeError("No JSON found in output", stdout, 0)
        data = json.loads(json_line)
    except json.JSONDecodeError:
        raise TestError(
            "Instantiation test returned invalid JSON",
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    if not data.get("success"):
        error_details = "\n".join(
            f"  - {e['node']}: {e['error']}" for e in data.get("errors", [])
        )
        raise TestError(
            f"Node instantiation failed for {len(data.get('errors', []))} node(s)",
            error_details
        )

    ctx.log(f"All {len(data.get('instantiated', []))} node(s) instantiated successfully!")
    return ctx
