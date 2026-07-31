"""REGISTRATION level - Start server and check for import errors."""

from ...common.errors import TestError
from ..context import LevelContext


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
            cuda_mock_packages=list(ctx.cuda_packages),
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
    import_errors = server.get_import_errors()
    if import_errors:
        server.stop()
        error_msg = "\n".join(import_errors)
        raise TestError(
            f"Node import failed ({len(import_errors)} error(s))",
            error_msg
        )
    ctx.log("No import errors detected")

    # Get registered nodes
    object_info = api.get_object_info()
    registered_nodes = tuple(object_info.keys())
    ctx.log(f"Found {len(registered_nodes)} registered nodes")

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
