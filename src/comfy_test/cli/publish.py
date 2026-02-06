"""Publish commands for comfy-test CLI."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def cmd_publish(args) -> int:
    """Publish results to gh-pages."""
    from ..reporting.html_report import generate_html_report

    results_dir = Path(args.results_dir).expanduser()
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}", file=sys.stderr)
        return 1

    results_file = results_dir / "results.json"
    if not results_file.exists():
        print(f"No results.json found in {results_dir}", file=sys.stderr)
        return 1

    repo = args.repo

    # Generate HTML report
    print("Generating HTML report...")
    generate_html_report(results_dir)

    # Push to gh-pages
    print(f"Publishing to gh-pages for {repo}...")

    with tempfile.TemporaryDirectory() as tmp:
        gh_pages_dir = Path(tmp) / "gh-pages"

        # Try to clone existing gh-pages branch
        clone_result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch=gh-pages",
             f"https://github.com/{repo}.git", str(gh_pages_dir)],
            capture_output=True
        )

        if clone_result.returncode != 0:
            # No gh-pages branch exists, create empty dir
            print("No existing gh-pages branch, creating new one...")
            gh_pages_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=gh_pages_dir, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "gh-pages"], cwd=gh_pages_dir, capture_output=True)

        # Clear old content (except .git)
        for item in gh_pages_dir.iterdir():
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # Copy new content
        files_to_copy = ["results.json", "index.html"]
        dirs_to_copy = ["screenshots", "videos", "logs"]

        for f in files_to_copy:
            src = results_dir / f
            if src.exists():
                shutil.copy2(src, gh_pages_dir / f)

        for d in dirs_to_copy:
            src = results_dir / d
            if src.exists():
                shutil.copytree(src, gh_pages_dir / d)

        # Commit and push
        subprocess.run(["git", "add", "-A"], cwd=gh_pages_dir, check=True)

        # Check if there are changes to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=gh_pages_dir, capture_output=True, text=True
        )
        if not status.stdout.strip():
            print("No changes to publish")
            return 0

        subprocess.run(
            ["git", "commit", "-m", "Update test results"],
            cwd=gh_pages_dir, check=True
        )

        # Push (requires auth - user should have git credentials configured)
        push_result = subprocess.run(
            ["git", "push", "-f", f"https://github.com/{repo}.git", "gh-pages"],
            cwd=gh_pages_dir
        )

        if push_result.returncode != 0:
            print("Push failed. Make sure you have write access to the repo.")
            print("You may need to set up a GitHub token:")
            print("  git config --global credential.helper store")
            print("  # Then push manually once to save credentials")
            return 1

    print(f"Published to https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/")
    return 0


def add_publish_parser(subparsers):
    """Add the publish subcommand parser."""
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish test results to gh-pages",
    )
    publish_parser.add_argument(
        "results_dir",
        help="Directory with test results (e.g., ~/logs/SAM3DBody-1445)",
    )
    publish_parser.add_argument(
        "--repo", "-r",
        required=True,
        help="GitHub repo in format 'owner/repo'",
    )
    publish_parser.set_defaults(func=cmd_publish)
