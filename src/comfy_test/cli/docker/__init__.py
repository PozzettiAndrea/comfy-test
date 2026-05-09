"""`comfy-test docker` subcommand group.

Subcommands:
    build  — Build the comfy-test Linux/Windows GPU image. Auto-detects host OS.
    test   — Clone a node and run comfy-test inside the container (was `dockertest`).
"""

from .build import cmd_docker_build, add_docker_build_parser
from .test import cmd_docker_test, add_docker_test_parser


def add_docker_parser(subparsers):
    """Register the `docker` subcommand group."""
    p = subparsers.add_parser(
        "docker",
        help="Docker image lifecycle and isolated tests",
    )
    sp = p.add_subparsers(dest="docker_command", required=True)
    add_docker_build_parser(sp)
    add_docker_test_parser(sp)


__all__ = [
    "add_docker_parser",
    "cmd_docker_build",
    "cmd_docker_test",
]
