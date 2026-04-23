"""Publish commands for comfy-test CLI."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def cmd_publish(args) -> int:
    """Publish results to gh-pages.

    Expects results_dir to be a platform results directory containing
    results.json, with branch and platform inferred from the path:

        logs/node-2115/dev/linux-gpu/   →  branch=dev, platform=linux-gpu

    Only the target branch/platform directory is replaced on gh-pages;
    other platforms' results are preserved.
    """
    from ..reporting.html_report import (
        generate_html_report,
        generate_root_index,
        generate_branch_root_index,
    )

    results_dir = Path(args.results_dir).expanduser().resolve()
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}", file=sys.stderr)
        return 1

    results_file = results_dir / "results.json"
    if not results_file.exists():
        print(f"No results.json found in {results_dir}", file=sys.stderr)
        return 1

    repo = args.repo

    # Infer branch and platform from path (last two components)
    platform = results_dir.name
    branch = results_dir.parent.name

    if not platform or not branch:
        print(f"Cannot infer branch/platform from path: {results_dir}", file=sys.stderr)
        print("Expected path like: logs/node-2115/dev/linux-gpu/", file=sys.stderr)
        return 1

    print(f"Publishing {repo} → gh-pages/{branch}/{platform}/")

    # Generate HTML report for this platform's results
    print("Generating HTML report...")
    generate_html_report(results_dir, repo_name=repo, current_platform=platform)

    with tempfile.TemporaryDirectory() as tmp:
        gh_pages_dir = Path(tmp) / "gh-pages"

        # Clone existing gh-pages (preserves all other content)
        clone_result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch=gh-pages",
             f"https://github.com/{repo}.git", str(gh_pages_dir)],
            capture_output=True
        )

        if clone_result.returncode != 0:
            print("No existing gh-pages branch, creating new one...")
            gh_pages_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=gh_pages_dir, capture_output=True)
            subprocess.run(
                ["git", "checkout", "-b", "gh-pages"],
                cwd=gh_pages_dir, capture_output=True,
            )

        # Replace only target branch/platform directory
        dest = gh_pages_dir / branch / platform
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        # Copy results
        for item in results_dir.iterdir():
            if item.name.startswith("."):
                continue
            if item.is_dir():
                shutil.copytree(item, dest / item.name)
            else:
                shutil.copy2(item, dest / item.name)

        # Ensure .nojekyll exists
        (gh_pages_dir / ".nojekyll").touch()

        # Regenerate index pages
        branch_dir = gh_pages_dir / branch
        if branch_dir.exists():
            print(f"Generating branch index: {branch_dir}")
            generate_root_index(branch_dir, repo_name=repo)

        print(f"Generating root index: {gh_pages_dir}")
        generate_branch_root_index(gh_pages_dir, repo_name=repo)

        # Commit and push
        subprocess.run(["git", "add", "-A"], cwd=gh_pages_dir, check=True)

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=gh_pages_dir, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            print("No changes to publish")
            return 0

        subprocess.run(
            ["git", "commit", "-m", f"Update {platform} results ({branch})"],
            cwd=gh_pages_dir, check=True,
        )

        push_result = subprocess.run(
            ["git", "push", "-f", f"https://github.com/{repo}.git", "gh-pages"],
            cwd=gh_pages_dir,
        )

        if push_result.returncode != 0:
            print("Push failed. Make sure you have write access to the repo.")
            print("Set up auth with one of:")
            print("  - GH_TOKEN or GITHUB_TOKEN env var + git credential helper")
            print("  - git config --global credential.helper store")
            return 1

    owner = repo.split("/")[0]
    repo_name_short = repo.split("/")[1]
    print(f"Published to https://{owner}.github.io/{repo_name_short}/{branch}/")
    return 0


def add_publish_parser(subparsers):
    """Add the publish subcommand parser."""
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish test results to gh-pages",
    )
    publish_parser.add_argument(
        "results_dir",
        help="Platform results directory containing results.json "
             "(e.g., logs/node-2115/dev/linux-gpu)",
    )
    publish_parser.add_argument(
        "--repo", "-r",
        required=True,
        help="GitHub repo in format 'owner/repo'",
    )
    publish_parser.set_defaults(func=cmd_publish)
