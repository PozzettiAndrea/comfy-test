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
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    from ...comfyui.server import ComfyUIServer, ExternalComfyUIServer

    # Start or connect to server
    if ctx.server_url:
        ctx.log(f"\nConnecting to existing server at {ctx.server_url}...")
        server = ExternalComfyUIServer(ctx.server_url, log_callback=ctx.log)
    else:
        ctx.log("\nStarting ComfyUI server...")
        server = ComfyUIServer(
            ctx.platform,
            ctx.paths,
            ctx.config,
            cuda_mock_packages=list(ctx.cuda_packages),
            log_callback=ctx.log,
            env_vars=ctx.env_vars,
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

    return ctx.with_updates(
        server=server,
        api=api,
        registered_nodes=registered_nodes,
    )
