"""Publish commands for comfy-test CLI."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _detect_repo() -> str | None:
    """Detect owner/repo from git remote origin URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        # Match github.com/owner/repo from HTTPS or SSH URLs
        m = re.search(r"github\.com[:/](.+?/.+?)(?:\.git)?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


def _find_result_dirs(base: Path) -> list[Path]:
    """Find all directories containing results.json under base.

    Returns paths like: base/dev/macos-cpu (containing results.json)
    """
    return sorted(p.parent for p in base.rglob("results.json"))


def _publish_platforms(
    platforms: list[tuple[str, str, Path]],  # [(branch, platform, results_dir), ...]
    repo: str,
    generate_html_report,
    generate_root_index,
    generate_branch_root_index,
) -> int:
    """Publish one or more platform results to gh-pages in a single push."""

    # Generate HTML reports for each platform
    for branch, platform, results_dir in platforms:
        print(f"Generating HTML report for {branch}/{platform}...")
        generate_html_report(results_dir, repo_name=repo, current_platform=platform)

    with tempfile.TemporaryDirectory() as tmp:
        gh_pages_dir = Path(tmp) / "gh-pages"

        # Clone existing gh-pages (preserves all other content)
        clone_result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch=gh-pages",
             f"https://github.com/{repo}.git", str(gh_pages_dir)],
            capture_output=True,
        )

        if clone_result.returncode != 0:
            print("No existing gh-pages branch, creating new one...")
            gh_pages_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=gh_pages_dir, capture_output=True)
            subprocess.run(
                ["git", "checkout", "-b", "gh-pages"],
                cwd=gh_pages_dir, capture_output=True,
            )

        # Replace each platform directory
        branches_touched = set()
        labels = []
        for branch, platform, results_dir in platforms:
            dest = gh_pages_dir / branch / platform
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)

            for item in results_dir.iterdir():
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    shutil.copytree(item, dest / item.name)
                else:
                    shutil.copy2(item, dest / item.name)

            branches_touched.add(branch)
            labels.append(f"{branch}/{platform}")
            print(f"  Updated {branch}/{platform}")

        # Ensure .nojekyll exists
        (gh_pages_dir / ".nojekyll").touch()

        # Regenerate index pages for touched branches
        for branch in branches_touched:
            branch_dir = gh_pages_dir / branch
            if branch_dir.exists():
                generate_root_index(branch_dir, repo_name=repo)

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

        msg = "Update " + ", ".join(labels)
        subprocess.run(
            ["git", "commit", "-m", msg],
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

    owner, repo_name = repo.split("/")
    for branch in branches_touched:
        print(f"Published to https://{owner}.github.io/{repo_name}/{branch}/")
    return 0


def cmd_publish(args) -> int:
    """Publish results to gh-pages.

    Accepts either:
      - A platform results dir:  logs/SAM3-2346/dev/macos-cpu
      - A parent log dir:        logs/SAM3-2346  (finds all platforms inside)

    If --repo is not provided, detects from git remote origin.
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

    # Detect repo
    repo = args.repo
    if not repo:
        repo = _detect_repo()
        if not repo:
            print("Cannot detect repo. Use --repo or run from a git repo.", file=sys.stderr)
            return 1
        print(f"Detected repo: {repo}")

    # Find platform result dirs
    if (results_dir / "results.json").exists():
        # Direct platform dir
        platform = results_dir.name
        branch = results_dir.parent.name
        if not platform or not branch:
            print(f"Cannot infer branch/platform from path: {results_dir}", file=sys.stderr)
            return 1
        platforms = [(branch, platform, results_dir)]
    else:
        # Parent dir — search for results inside
        result_dirs = _find_result_dirs(results_dir)
        if not result_dirs:
            print(f"No results.json found in {results_dir}", file=sys.stderr)
            return 1
        platforms = []
        for rd in result_dirs:
            platform = rd.name
            branch = rd.parent.name
            platforms.append((branch, platform, rd))
        print(f"Found {len(platforms)} platform(s): {', '.join(f'{b}/{p}' for b, p, _ in platforms)}")

    print(f"Publishing to {repo} gh-pages...")
    return _publish_platforms(
        platforms, repo,
        generate_html_report, generate_root_index, generate_branch_root_index,
    )


def add_publish_parser(subparsers):
    """Add the publish subcommand parser."""
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish test results to gh-pages",
    )
    publish_parser.add_argument(
        "results_dir",
        help="Results directory — either a platform dir (logs/SAM3/dev/macos-cpu) "
             "or parent dir (logs/SAM3, finds all platforms inside)",
    )
    publish_parser.add_argument(
        "--repo", "-r",
        default=None,
        help="GitHub repo (owner/repo). Auto-detected from git remote if omitted.",
    )
    publish_parser.set_defaults(func=cmd_publish)
