"""INSTANTIATION level - Test node constructors."""

import json
import os
import subprocess

from ...common.errors import TestError
from ...common.comfy_env import get_cuda_packages
from ..context import LevelContext


# Script template for testing node instantiation in subprocess
INSTANTIATION_SCRIPT = '''
import sys
import os
import json
from pathlib import Path

# Disable CUDA on CPU-only machines to prevent crashes
# (model_management.py calls torch.cuda at import time)
is_gpu_runner = {is_gpu_runner}
if not is_gpu_runner:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    # On Windows, CUDA_VISIBLE_DEVICES="" is not enough - torch.cuda C++ calls
    # can still crash with access violations when no GPU driver is present.
    # Monkey-patch torch.cuda before any ComfyUI imports to prevent this.
    try:
        import torch
        torch.cuda.is_available = lambda: False
        torch.cuda.device_count = lambda: 0
        torch.cuda.current_device = lambda: 0
    except ImportError:
        pass

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
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    print(f"IMPORT ERROR: {{e}}", flush=True)
    print(tb, flush=True)
    print(json.dumps({{"success": False, "error": f"Failed to import {{node_name}}: {{e}}", "traceback": tb}}))
    sys.exit(1)

# Get NODE_CLASS_MAPPINGS and run instantiation with full error capture
try:
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
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    print(f"FATAL ERROR: {{e}}", flush=True)
    print(tb, flush=True)
    print(json.dumps({{"success": False, "error": str(e), "traceback": tb}}))
    sys.exit(1)
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
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    ctx.log("Testing node constructors...")

    # Get CUDA packages if not already set (e.g., when INSTALL was skipped)
    cuda_packages = ctx.cuda_packages
    if not cuda_packages and not os.environ.get("COMFY_TEST_GPU"):
        cuda_packages = tuple(get_cuda_packages(ctx.node_dir))
        if cuda_packages:
            ctx.log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

    # Build the test script
    is_gpu_runner = os.environ.get("COMFY_TEST_GPU") == "1"
    script = INSTANTIATION_SCRIPT.format(
        custom_nodes_dir=str(ctx.paths.custom_nodes_dir).replace("\\", "/"),
        node_name=ctx.node_dir.name,
        cuda_packages_json=json.dumps(list(cuda_packages)),
        is_gpu_runner="True" if is_gpu_runner else "False",
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
