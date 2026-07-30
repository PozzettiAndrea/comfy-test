"""`comfy-test generate-index` -- render the gh-pages index over a staged tree.

The publish workflows own the gh-pages branch mechanics (checkout, peaceiris
push) and only need the HTML re-rendered over the already-staged directory --
that is what this command is for. The same reporting functions run inside
`comfy-test publish` for the single-machine path, so this is a thin CLI shim
over `reporting.html_report`, not logic of its own.
"""

from pathlib import Path

from comfy_test.reporting.html_report import (
    PLATFORMS,
    generate_html_report,
    generate_root_index,
    generate_branch_root_index,
)


def cmd_generate_index(args) -> int:
    """Render the gh-pages index tree.

    Given the gh-pages ROOT and (optionally) a --branch, renders:
      <root>/<branch>/<platform>/index.html  per-platform reports
      <root>/<branch>/index.html             branch index (platform tabs)
      <root>/index.html                      root index (branch switcher)
    """
    root = Path(args.output_dir)
    if not root.exists():
        print(f"Error: Directory does not exist: {root}")
        return 1

    if args.branch:
        branch_dir = root / args.branch
        if branch_dir.exists():
            # Per-platform: render the report for any subdir whose name matches
            # a known PLATFORMS id and contains a results.json (covers desktop
            # runs whose output is just dropped into the tree by the workflow).
            platform_ids = {p["id"] for p in PLATFORMS}
            for sub in sorted(branch_dir.iterdir()):
                if not sub.is_dir() or sub.name not in platform_ids:
                    continue
                if not (sub / "results.json").exists():
                    print(f"Skipping {sub.name}: no results.json")
                    continue
                try:
                    per_index = generate_html_report(
                        sub, repo_name=args.repo_name, current_platform=sub.name)
                    print(f"Generated: {per_index}")
                except Exception as e:
                    print(f"Failed to render {sub.name}: {e}")
            print(f"Generated: {generate_root_index(branch_dir, args.repo_name)}")
        else:
            print(f"Warning: branch dir does not exist: {branch_dir}")

    # Root index (branch switcher) -- always, so a new branch shows up.
    print(f"Generated: {generate_branch_root_index(root, args.repo_name)}")
    return 0


def add_generate_index_parser(subparsers):
    """Register the generate-index subcommand."""
    parser = subparsers.add_parser(
        "generate-index",
        help="Render the gh-pages index (branch platform-tabs + root branch-switcher)",
    )
    parser.add_argument(
        "output_dir",
        help="gh-pages ROOT directory (contains per-branch subdirs), e.g. gh-pages",
    )
    parser.add_argument(
        "--branch",
        help="Branch subdir to render per-platform reports + the branch index for",
    )
    parser.add_argument(
        "--repo-name",
        help="Repository name for header (e.g., owner/repo)",
    )
    parser.set_defaults(func=cmd_generate_index)
