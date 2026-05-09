"""Helpers for the `<url-or-path>` positional accepted by `comfy-test run`
and `comfy-test docker test`.

Resolution rules:
- `owner/repo`              → expanded to `https://github.com/owner/repo.git`
- existing local directory  → used as-is (no clone)
- anything else             → treated as a remote URL (cloned shallowly)
"""

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def expand_nodelink(nodelink: str) -> str:
    """Expand `owner/repo` shorthand to a full GitHub URL. Pass-through otherwise."""
    if Path(nodelink).exists() or "://" in nodelink or nodelink.count("/") != 1:
        return nodelink
    owner, repo = nodelink.split("/", 1)
    if not owner or not repo:
        return nodelink
    return f"https://github.com/{owner}/{repo}.git"


def is_url_nodelink(nodelink: str) -> bool:
    """True if nodelink is a remote URL (or owner/repo shorthand), not a local dir."""
    expanded = expand_nodelink(nodelink)
    p = Path(expanded)
    return not (p.exists() and p.is_dir())


def node_name_from_url(nodelink: str) -> str:
    """Derive the node directory name from a URL (or owner/repo shorthand)."""
    expanded = expand_nodelink(nodelink)
    return expanded.rstrip("/").split("/")[-1].removesuffix(".git")


def clone_node(nodelink: str, branch: Optional[str], dest: Path,
               log_prefix: str = "[nodelink]") -> str:
    """Shallow-clone nodelink into dest/<name>, return the name. Raises on failure."""
    expanded = expand_nodelink(nodelink)
    name = node_name_from_url(expanded)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    if target.exists():
        shutil.rmtree(target)
    branch_desc = f"branch={branch}" if branch else "default branch"
    print(f"{log_prefix} clone {expanded} ({branch_desc}) → {target}")
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([expanded, str(target)])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{r.stderr}")
    sha = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    if sha.returncode == 0:
        short = sha.stdout.strip()[:12]
        msg = subprocess.run(["git", "-C", str(target), "log", "-1", "--format=%s (%ci)"],
                             capture_output=True, text=True)
        subj = msg.stdout.strip() if msg.returncode == 0 else ""
        print(f"{log_prefix} cloned {name} @ {short}  {subj}")
    return name


def copy_local_node(nodelink: str, dest: Path,
                    log_prefix: str = "[nodelink]") -> str:
    """Copy a local node directory into dest/<name>, return the name."""
    nodelink = expand_nodelink(nodelink)
    src_path = Path(nodelink)
    if not src_path.exists():
        raise RuntimeError(f"Local path not found: {nodelink}")
    if not src_path.is_dir():
        raise RuntimeError(f"Local path is not a directory: {nodelink}")
    name = src_path.name
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    if target.exists():
        shutil.rmtree(target)
    print(f"{log_prefix} LOCAL PATH → copying {src_path} to {target}")
    shutil.copytree(src_path, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".venv", "venv", ".git",
                                                  "__pycache__", ".comfy-test"))
    return name
