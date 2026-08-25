"""Publish commands for comfy-test CLI."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._git_auth import authenticated_github_url, git_env as _shared_git_env


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


_GIT_TIMEOUT_SECONDS = 300


def _git_env() -> dict:
    return _shared_git_env()


def _git_remote_url(repo: str) -> str:
    """HTTPS URL for the repo, with embedded token if one is in env.

    Delegates to the shared helper so the test path (cloning node repos) and
    the publish path (pushing gh-pages) use one URL-rewrite. The shared helper
    accepts 'owner/repo' shorthand; we forward verbatim.
    """
    return authenticated_github_url(repo)


def _publish_lanes(
    lanes: list[tuple[str, str, Path]],  # [(branch, lane, results_dir), ...]
    repo: str,
    generate_html_report,
    regenerate_tree,
) -> int:
    """Publish one or more lane results to gh-pages in a single push."""

    # Generate HTML reports for each lane
    for branch, lane, results_dir in lanes:
        print(f"Generating HTML report for {branch}/{lane}...")
        generate_html_report(results_dir, repo_name=repo)

    remote_url = _git_remote_url(repo)
    git_env = _git_env()

    with tempfile.TemporaryDirectory() as tmp:
        gh_pages_dir = Path(tmp) / "gh-pages"

        # Clone existing gh-pages (preserves all other content)
        try:
            clone_result = subprocess.run(
                ["git", "clone", "--depth=1", "--branch=gh-pages",
                 remote_url, str(gh_pages_dir)],
                capture_output=True, text=True, env=git_env,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(
                f"git clone of gh-pages timed out after {_GIT_TIMEOUT_SECONDS}s. "
                "Check network and credentials.",
                file=sys.stderr,
            )
            return 1

        if clone_result.returncode != 0:
            stderr = (clone_result.stderr or "").strip()
            # A missing gh-pages branch is fine; any other failure is fatal.
            if "Remote branch gh-pages not found" in stderr or "couldn't find remote ref" in stderr.lower():
                print("No existing gh-pages branch, creating new one...")
                gh_pages_dir.mkdir(parents=True)
                subprocess.run(["git", "init"], cwd=gh_pages_dir, capture_output=True, env=git_env)
                subprocess.run(
                    ["git", "checkout", "-b", "gh-pages"],
                    cwd=gh_pages_dir, capture_output=True, env=git_env,
                )
            else:
                print(f"git clone failed: {stderr}", file=sys.stderr)
                return 1

        # Replace each lane directory
        branches_touched = set()
        labels = []
        for branch, lane, results_dir in lanes:
            dest = gh_pages_dir / branch / lane
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
            labels.append(f"{branch}/{lane}")
            print(f"  Updated {branch}/{lane}")

        # Ensure .nojekyll exists
        (gh_pages_dir / ".nojekyll").touch()

        # Re-render the WHOLE tree, not just the branches this run touched.
        # The three frame levels are published at different times, so a partial
        # regeneration leaves pages from different comfy-test versions talking
        # to each other over postMessage -- silently degrading deep links and
        # lane switching. Every lane dir keeps its results.json, so the whole
        # tree is re-renderable from what is already on disk.
        regenerate_tree(gh_pages_dir, repo_name=repo, log=print)

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

        try:
            push_result = subprocess.run(
                ["git", "push", "-f", remote_url, "gh-pages"],
                cwd=gh_pages_dir, env=git_env,
                capture_output=True, text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(
                f"git push timed out after {_GIT_TIMEOUT_SECONDS}s. "
                "Likely a stuck credential prompt or network stall.",
                file=sys.stderr,
            )
            return 1

        if push_result.stdout:
            sys.stdout.write(push_result.stdout)
        if push_result.stderr:
            sys.stderr.write(push_result.stderr)
        if push_result.returncode != 0:
            print("Push failed. Make sure you have write access to the repo.")
            print("Set up auth with one of:")
            print("  - GH_TOKEN, GITHUB_TOKEN, or NODE_PAT env var (preferred)")
            print("  - git config --global credential.helper store + ~/.git-credentials")
            return 1

    owner, repo_name = repo.split("/")
    for branch in branches_touched:
        print(f"Published to https://{owner}.github.io/{repo_name}/{branch}/")
    return 0


def cmd_publish(args) -> int:
    """Publish results to gh-pages.

    Accepts either:
      - A lane results dir:  logs/SAM3-2346/dev/macos-cpu
      - A parent log dir:        logs/SAM3-2346  (finds all lanes inside)

    If --repo is not provided, detects from git remote origin.
    """
    from ..reporting.html_report import (
        generate_html_report,
        regenerate_tree,
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

    # Find lane result dirs
    if (results_dir / "results.json").exists():
        # Direct lane dir
        lane = results_dir.name
        branch = results_dir.parent.name
        if not lane or not branch:
            print(f"Cannot infer branch/lane from path: {results_dir}", file=sys.stderr)
            return 1
        lanes = [(branch, lane, results_dir)]
    else:
        # Parent dir -- search for results inside
        result_dirs = _find_result_dirs(results_dir)
        if not result_dirs:
            print(f"No results.json found in {results_dir}", file=sys.stderr)
            return 1
        lanes = []
        for rd in result_dirs:
            lane = rd.name
            branch = rd.parent.name
            lanes.append((branch, lane, rd))
        print(f"Found {len(lanes)} lane(s): {', '.join(f'{b}/{p}' for b, p, _ in lanes)}")

    print(f"Publishing to {repo} gh-pages...")
    return _publish_lanes(
        lanes, repo,
        generate_html_report, regenerate_tree,
    )


def add_publish_parser(subparsers):
    """Add the publish subcommand parser."""
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish test results to gh-pages",
    )
    publish_parser.add_argument(
        "results_dir",
        help="Results directory -- either a lane dir (logs/SAM3/dev/macos-cpu) "
             "or parent dir (logs/SAM3, finds all lanes inside)",
    )
    publish_parser.add_argument(
        "--repo", "-r",
        default=None,
        help="GitHub repo (owner/repo). Auto-detected from git remote if omitted.",
    )
    publish_parser.set_defaults(func=cmd_publish)
