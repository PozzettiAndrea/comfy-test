"""REGISTRATION level - Start server and check for import errors."""

from ...common.errors import TestError
from ..context import LevelContext


def attribute_own_nodes(object_info: dict, pack_name: str):
    """Split /object_info into (this pack's node names, any-attribution-seen).

    A clean import is not a registration. `load_custom_node` takes the V1
    branch on any non-None NODE_CLASS_MAPPINGS -- an empty dict included --
    iterates nothing and returns True (nodes.py:2292-2301), so upstream
    reports success for a pack that contributes zero nodes. Counting all of
    /object_info hides that: it is ~850 entries of ComfyUI's own builtins and
    says nothing about the pack under test.

    Attribution is upstream's own: /object_info carries `python_module`
    (server.py:765) from `RELATIVE_PYTHON_MODULE`, built as
    "custom_nodes.<dir>" (nodes.py:2296) -- and a pack is always identified by
    its custom_nodes directory name, which is what config.name holds.

    The second return value distinguishes "registered nothing" from "this
    server does not report attribution at all", which must not be a failure.
    """
    pack_module = f"custom_nodes.{pack_name}"
    attributed = any(i.get("python_module") for i in object_info.values())
    own = tuple(
        name for name, info in object_info.items()
        if info.get("python_module") == pack_module
    )
    return own, attributed


def run(ctx: LevelContext) -> LevelContext:
    """Run REGISTRATION level.

    Starts the ComfyUI server and checks for import errors in the server logs.
    Also retrieves the list of registered nodes for later levels.

    Args:
        ctx: Level context (must have platform, paths, cuda_packages set)

    Returns:
        Updated context with server, api, registered_nodes

    Raises:
        TestError: If node import fails
    """
    ctx.log(f"\n[DEBUG] server={ctx.server}, api={ctx.api}")
    from ...comfyui.server import ComfyUIServer, AttachedServer

    if ctx.server_url:
        # Attach mode: CI already booted the server; wrap it instead of
        # starting our own. Startup output comes from the workflow's
        # server.log (COMFY_TEST_LOGS_DIR), so the import-error check below
        # keeps identical semantics.
        import os
        from pathlib import Path
        log_file = None
        logs_dir = os.environ.get("COMFY_TEST_LOGS_DIR")
        if logs_dir and (Path(logs_dir) / "server.log").exists():
            log_file = Path(logs_dir) / "server.log"
        ctx.log(f"\nAttaching to ComfyUI server at {ctx.server_url} "
                f"(log: {log_file or 'unavailable'})...")
        server = AttachedServer(ctx.server_url, log_file, log_callback=ctx.log)
        api = server.get_api()
        if not api.health_check():
            raise TestError(
                f"No ComfyUI server responding at {ctx.server_url}",
                "Attach mode expects CI to have booted the server before "
                "invoking comfy-test run --server-url.",
            )
    else:
        ctx.log("\nStarting ComfyUI server...")
        server = ComfyUIServer(
            ctx.platform,
            ctx.paths,
            ctx.config,
            log_callback=ctx.log,
            env_vars=ctx.env_vars,
            novram=ctx.novram,
            vram_debug=ctx.vram_debug,
        )

        # Start the server (enters context manager)
        server.start()
        api = server.get_api()

    # Check for import errors
    ctx.log("Checking for import errors in server logs...")
    # Scoped to this pack: ComfyUI logs "IMPORT FAILED" for its own
    # comfy_extras / comfy_api_nodes too, and those failed the pack for
    # something its author cannot fix.
    import_errors = server.get_import_errors(ctx.config.name)
    if import_errors:
        server.stop()
        raise TestError(
            f"Node import failed ({len(import_errors)} error(s))",
            "\n".join(import_errors)
        )
    ctx.log("No import errors detected")

    # Get registered nodes
    object_info = api.get_object_info()
    registered_nodes = tuple(object_info.keys())
    ctx.log(f"Found {len(registered_nodes)} registered nodes (all packs)")

    # ...of which, how many are OURS?
    own_nodes, attributed = attribute_own_nodes(object_info, ctx.config.name)
    if not attributed:
        # No entry carries the field: a ComfyUI predating server.py:765.
        # Unknown attribution is not zero nodes, so do not fail on it.
        ctx.log("WARNING: /object_info reports no python_module; cannot verify "
                "that this pack registered anything")
    elif not own_nodes:
        server.stop()
        raise TestError(
            "Imported cleanly but registered 0 nodes",
            f"No entry in /object_info is attributed to "
            f"'custom_nodes.{ctx.config.name}'.\n"
            f"The pack imported without error, so ComfyUI called this a "
            f"success -- an empty NODE_CLASS_MAPPINGS still returns True.\n"
            f"Usual causes: __init__.py does not export NODE_CLASS_MAPPINGS, "
            f"exports it empty, or builds it under a condition that was false "
            f"here (a missing optional dependency, an accelerator check)."
        )
    else:
        ctx.log(f"Registered {len(own_nodes)} nodes from this pack: "
                f"{', '.join(sorted(own_nodes)[:10])}"
                f"{' ...' if len(own_nodes) > 10 else ''}")

    # Provenance: the running server's own version report is authoritative
    # (overrides the pyproject read from INSTALL).
    comfyui_version = (api.get_system_stats().get("system") or {}).get("comfyui_version") \
        or ctx.comfyui_version
    if comfyui_version:
        ctx.log(f"ComfyUI version (server-reported): {comfyui_version}")

    return ctx.with_updates(
        server=server,
        api=api,
        registered_nodes=registered_nodes,
        comfyui_version=comfyui_version,
    )
