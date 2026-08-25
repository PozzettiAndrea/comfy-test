"""INSTANTIATION level - Test node constructors."""

import json
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
is_cuda_runner = {is_cuda_runner}
if not is_cuda_runner:
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
from ...common.accel import is_cuda_run

# Add custom_nodes directory to sys.path for proper package imports
custom_nodes_dir = Path("{custom_nodes_dir}")
if str(custom_nodes_dir) not in sys.path:
    sys.path.insert(0, str(custom_nodes_dir))

# Import the node as a proper package
node_name = "{node_name}"
import traceback

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

# Collect the node classes the way ComfyUI does (nodes.py:2292-2331): a pack
# declares NODE_CLASS_MAPPINGS **or** a V3 comfy_entrypoint. Reading only the
# former meant a V3-only pack contributed zero nodes and the level reported
# "All 0 node(s) instantiated successfully!" -- a pass that proved nothing.
try:
    mappings = getattr(module, "NODE_CLASS_MAPPINGS", None) or {{}}
    api_version = "v1" if mappings else None

    if not mappings and hasattr(module, "comfy_entrypoint"):
        api_version = "v3"
        import asyncio
        import inspect as _inspect

        def _load_v3():
            entrypoint = getattr(module, "comfy_entrypoint")
            if not callable(entrypoint):
                raise RuntimeError("comfy_entrypoint is not callable")

            async def _go():
                ext = (await entrypoint() if _inspect.iscoroutinefunction(entrypoint)
                       else entrypoint())
                await ext.on_load()
                return await ext.get_node_list()

            node_list = asyncio.run(_go())
            if not isinstance(node_list, list):
                raise RuntimeError("get_node_list() did not return a list")
            out = {{}}
            for node_cls in node_list:
                out[node_cls.GET_SCHEMA().node_id] = node_cls
            return out

        try:
            mappings = _load_v3()
            print(f"Loaded {{len(mappings)}} node(s) via comfy_entrypoint (V3)", flush=True)
        except Exception as e:
            tb = traceback.format_exc() if "traceback" in dir() else str(e)
            print(json.dumps({{"success": False, "api_version": "v3",
                              "error": f"comfy_entrypoint failed: {{e}}",
                              "traceback": tb}}))
            sys.exit(1)

    if api_version is None:
        print(json.dumps({{"success": False, "api_version": None, "errors": [],
                           "error": "Pack declares neither NODE_CLASS_MAPPINGS "
                                    "nor comfy_entrypoint"}}))
        sys.exit(1)

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
        "api_version": api_version,
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
    ctx.log(f"\n[DEBUG] server={ctx.server}, api={ctx.api}")
    ctx.log("Testing node constructors...")

    # Get CUDA packages if not already set (e.g., when INSTALL was skipped)
    cuda_packages = ctx.cuda_packages
    if not cuda_packages and not is_cuda_run():
        cuda_packages = tuple(get_cuda_packages(ctx.node_dir))
        if cuda_packages:
            ctx.log(f"Found CUDA packages to mock: {', '.join(cuda_packages)}")

    # Build the test script
    is_cuda_runner = is_cuda_run()
    script = INSTANTIATION_SCRIPT.format(
        custom_nodes_dir=str(ctx.paths.custom_nodes_dir).replace("\\", "/"),
        node_name=ctx.node_dir.name,
        cuda_packages_json=json.dumps(list(cuda_packages)),
        is_cuda_runner="True" if is_cuda_runner else "False",
    )

    # Run the script
    result = subprocess.run(
        [str(ctx.paths.python), "-c", script],
        cwd=str(ctx.paths.comfyui_dir),
        capture_output=True,
        text=True,
        timeout=240,
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

    instantiated = data.get("instantiated", [])
    api_version = data.get("api_version")
    if not instantiated:
        # "All 0 node(s) instantiated successfully" was a pass that proved
        # nothing. Reaching here means the pack declared an API surface (the
        # subprocess exits non-zero otherwise) but it yielded no classes.
        raise TestError(
            "Imported cleanly but exposed 0 instantiable nodes",
            f"The pack declares a {api_version or 'node'} API surface, but it "
            f"produced no node classes to instantiate.\n"
            f"Usual causes: NODE_CLASS_MAPPINGS is built under a condition that "
            f"was false here (a missing optional dependency, an accelerator "
            f"check), or a V3 get_node_list() returned an empty list."
        )
    ctx.log(f"All {len(instantiated)} node(s) instantiated successfully! "
            f"({api_version or 'unknown'} API)")
    return ctx
