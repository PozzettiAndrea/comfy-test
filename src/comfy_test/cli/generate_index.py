"""Generate index commands for comfy-test CLI."""

from pathlib import Path

from comfy_test.reporting.html_report import (
    PLATFORMS,
    generate_html_report,
    generate_root_index,
    generate_branch_root_index,
)


def cmd_generate_index(args) -> int:
    """Generate branch index with platform tabs.

    Also generates per-platform index.html for any platform subdir
    that has a results.json — needed for desktop runs whose output
    is just dropped into the gh-pages tree by the publish workflow.
    """
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Error: Directory does not exist: {output_dir}")
        return 1

    # Per-platform: render the report for any subdir whose name matches
    # a known PLATFORMS id and contains a results.json.
    platform_ids = {p['id'] for p in PLATFORMS}
    for sub in sorted(output_dir.iterdir()):
        if not sub.is_dir() or sub.name not in platform_ids:
            continue
        results = sub / "results.json"
        if not results.exists():
            print(f"Skipping {sub.name}: no results.json")
            continue
        try:
            per_index = generate_html_report(sub, repo_name=args.repo_name,
                                             current_platform=sub.name)
            print(f"Generated: {per_index}")
        except Exception as e:
            print(f"Failed to render {sub.name}: {e}")

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
