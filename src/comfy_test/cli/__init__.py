"""CLI commands for comfy-test.

This module provides the command-line interface for comfy-test.
"""

import argparse
import sys

from .run import cmd_run, add_run_parser
from .init import cmd_init, add_init_parser
from .publish import cmd_publish, add_publish_parser
from .paths import cmd_paths, add_paths_parser
from .generate_index import (
    cmd_generate_index,
    cmd_generate_root_index,
    add_generate_index_parser,
    add_generate_root_index_parser,
)


def main(args=None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="comfy-test",
        description="Installation testing for ComfyUI custom nodes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Register commands
    add_run_parser(subparsers)
    add_init_parser(subparsers)
    add_publish_parser(subparsers)
    add_paths_parser(subparsers)
    add_generate_index_parser(subparsers)
    add_generate_root_index_parser(subparsers)

    # Parse and execute
    parsed_args = parser.parse_args(args)
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
