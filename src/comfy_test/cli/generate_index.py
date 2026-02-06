"""Generate index commands for comfy-test CLI."""

from pathlib import Path

from comfy_test.reporting.html_report import (
    generate_root_index,
    generate_branch_root_index,
)


def cmd_generate_index(args) -> int:
    """Generate branch index with platform tabs."""
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Error: Directory does not exist: {output_dir}")
        return 1

    index_path = generate_root_index(output_dir, args.repo_name)
    print(f"Generated: {index_path}")
    return 0


def cmd_generate_root_index(args) -> int:
    """Generate root index with branch switcher."""
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Error: Directory does not exist: {output_dir}")
        return 1

    index_path = generate_branch_root_index(output_dir, args.repo_name)
    print(f"Generated: {index_path}")
    return 0


def add_generate_index_parser(subparsers):
    """Add the generate-index subcommand parser."""
    parser = subparsers.add_parser(
        "generate-index",
        help="Generate branch index.html with platform tabs",
    )
    parser.add_argument(
        "output_dir",
        help="Directory to generate index in (e.g., gh-pages/main)",
    )
    parser.add_argument(
        "--repo-name",
        help="Repository name for header (e.g., owner/repo)",
    )
    parser.set_defaults(func=cmd_generate_index)


def add_generate_root_index_parser(subparsers):
    """Add the generate-root-index subcommand parser."""
    parser = subparsers.add_parser(
        "generate-root-index",
        help="Generate root index.html with branch switcher",
    )
    parser.add_argument(
        "output_dir",
        help="Root directory containing branch subdirectories (e.g., gh-pages)",
    )
    parser.add_argument(
        "--repo-name",
        help="Repository name for header (e.g., owner/repo)",
    )
    parser.set_defaults(func=cmd_generate_root_index)
