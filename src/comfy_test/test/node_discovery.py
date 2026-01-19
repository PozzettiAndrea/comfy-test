"""Auto-discover nodes from custom node's NODE_CLASS_MAPPINGS."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import List

from ..errors import SetupError


def discover_nodes(node_path: Path) -> list[str]:
    """Import custom node and return NODE_CLASS_MAPPINGS keys.

    Args:
        node_path: Path to the custom node directory containing __init__.py

    Returns:
        List of node names from NODE_CLASS_MAPPINGS

    Raises:
        SetupError: If import fails or NODE_CLASS_MAPPINGS not found
    """
    init_file = node_path / "__init__.py"
    if not init_file.exists():
        raise SetupError(
            f"No __init__.py found in {node_path}",
            "Custom nodes must have an __init__.py that exports NODE_CLASS_MAPPINGS"
        )

    # Generate a unique module name to avoid conflicts
    module_name = f"_comfy_test_node_{node_path.name}"

    # Add node_path to sys.path temporarily
    node_path_str = str(node_path)
    if node_path_str not in sys.path:
        sys.path.insert(0, node_path_str)

    try:
        # Load the module from the __init__.py file
        spec = importlib.util.spec_from_file_location(module_name, init_file)
        if spec is None or spec.loader is None:
            raise SetupError(
                f"Failed to load module spec from {init_file}",
                "The __init__.py file could not be parsed"
            )

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise SetupError(
                f"Failed to import {node_path.name}",
                f"Import error: {e}"
            )

        # Get NODE_CLASS_MAPPINGS
        if not hasattr(module, "NODE_CLASS_MAPPINGS"):
            raise SetupError(
                f"NODE_CLASS_MAPPINGS not found in {node_path.name}",
                "Custom nodes must export NODE_CLASS_MAPPINGS in __init__.py"
            )

        node_class_mappings = getattr(module, "NODE_CLASS_MAPPINGS")
        if not isinstance(node_class_mappings, dict):
            raise SetupError(
                f"NODE_CLASS_MAPPINGS is not a dict in {node_path.name}",
                f"Expected dict, got {type(node_class_mappings).__name__}"
            )

        return list(node_class_mappings.keys())

    finally:
        # Clean up: remove from sys.modules and sys.path
        if module_name in sys.modules:
            del sys.modules[module_name]
        if node_path_str in sys.path:
            sys.path.remove(node_path_str)


def discover_nodes_subprocess(
    node_path: Path,
    python_path: Path,
    cuda_packages: List[str],
    comfyui_dir: Path,
) -> List[str]:
    """Import custom node in subprocess and return NODE_CLASS_MAPPINGS keys.

    This runs node discovery in the test venv's Python process, ensuring that
    dependencies installed in that venv (like numpy) are available.

    Args:
        node_path: Path to the custom node directory in the test environment
        python_path: Path to the Python executable in the test venv
        cuda_packages: List of CUDA packages to mock before importing
        comfyui_dir: Path to the ComfyUI directory (for folder_paths)

    Returns:
        List of node names from NODE_CLASS_MAPPINGS

    Raises:
        SetupError: If import fails or NODE_CLASS_MAPPINGS not found
    """
    # Build the discovery script
    # Use verbose logging with flush to diagnose crashes in native libraries
    script = '''
import sys
import json

def log(msg):
    """Print diagnostic message and flush to ensure output on crash."""
    print(f"[node_discovery] {{msg}}", file=sys.stderr)
    sys.stderr.flush()

log("Starting node discovery...")

# Mock CUDA packages if needed
cuda_packages = {cuda_packages_json}
for pkg in cuda_packages:
    if pkg not in sys.modules:
        import types
        sys.modules[pkg] = types.ModuleType(pkg)
        log(f"Mocked CUDA package: {{pkg}}")

log("Importing folder_paths...")
# Import ComfyUI's folder_paths to set up paths
import folder_paths

log("folder_paths imported successfully")

# Find and import the node module
import importlib.util
from pathlib import Path

node_dir = Path("{node_dir}")
init_file = node_dir / "__init__.py"

if not init_file.exists():
    print(json.dumps({{"success": False, "error": "No __init__.py found in " + str(node_dir)}}))
    sys.exit(1)

spec = importlib.util.spec_from_file_location("test_node", init_file)
if spec is None or spec.loader is None:
    print(json.dumps({{"success": False, "error": "Failed to load module spec from " + str(init_file)}}))
    sys.exit(1)

module = importlib.util.module_from_spec(spec)
sys.modules["test_node"] = module

log(f"Importing custom node from {{init_file}}...")
try:
    spec.loader.exec_module(module)
except Exception as e:
    import traceback
    tb = traceback.format_exc()
    print(json.dumps({{"success": False, "error": "Import error: " + str(e), "traceback": tb}}))
    sys.exit(1)

log("Custom node imported successfully")

# Get NODE_CLASS_MAPPINGS
if not hasattr(module, "NODE_CLASS_MAPPINGS"):
    print(json.dumps({{"success": False, "error": "NODE_CLASS_MAPPINGS not found"}}))
    sys.exit(1)

mappings = getattr(module, "NODE_CLASS_MAPPINGS")
if not isinstance(mappings, dict):
    print(json.dumps({{"success": False, "error": "NODE_CLASS_MAPPINGS is not a dict, got " + type(mappings).__name__}}))
    sys.exit(1)

log(f"Found {{len(mappings)}} nodes")
result = {{
    "success": True,
    "nodes": list(mappings.keys()),
}}
print(json.dumps(result))
'''.format(
        node_dir=str(node_path).replace("\\", "/"),
        cuda_packages_json=json.dumps(cuda_packages),
    )

    # Run the script in the test venv
    # Use -u for unbuffered output to capture output even on crashes
    try:
        result = subprocess.run(
            [str(python_path), "-u", "-c", script],
            cwd=str(comfyui_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise SetupError(
            "Node discovery timed out",
            "The node import took longer than 60 seconds"
        )

    if result.returncode != 0:
        raise SetupError(
            f"Failed to import custom node",
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    try:
        # Extract JSON from stdout (may have log messages before it)
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
        raise SetupError(
            "Node discovery returned invalid JSON",
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        raise SetupError(
            f"Failed to import custom node",
            f"Import error: {error}"
        )

    return data.get("nodes", [])
