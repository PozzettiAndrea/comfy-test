"""`comfy-test docker` subcommand group.

Subcommands:
    list   -- Show known images, whether loaded locally, and SMB artifact paths.
             Also the default action when `comfy-test docker` is run with no
             subcommand.
    build  -- Build the comfy-test Linux/Windows GPU image. Auto-detects host OS.
    run    -- Clone a node and run comfy-test inside the container (mirror of
             top-level `comfy-test run`, just dispatching via Docker).
"""

from .build import cmd_docker_build, add_docker_build_parser
from .list import cmd_docker_list, add_docker_list_parser
from .run import cmd_docker_run, add_docker_run_parser


def add_docker_parser(subparsers):
    """Register the `docker` subcommand group."""
    p = subparsers.add_parser(
        "docker",
        help="Docker image lifecycle and containerized test runs",
    )
    # Bare `comfy-test docker` (no subcommand) defaults to `list`.
    p.set_defaults(func=cmd_docker_list)
    sp = p.add_subparsers(dest="docker_command", required=False)
    add_docker_list_parser(sp)
    add_docker_build_parser(sp)
    add_docker_run_parser(sp)


__all__ = [
    "add_docker_parser",
    "cmd_docker_build",
    "cmd_docker_list",
    "cmd_docker_run",
]
