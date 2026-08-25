"""Helpers for the `<url-or-path>` positional accepted by `comfy-test run`
and `comfy-test docker test`.

Resolution rules:
- `owner/repo`              -> expanded to `https://github.com/owner/repo.git`
- existing local directory  -> used as-is (no clone)
- anything else             -> treated as a remote URL (cloned shallowly)
"""

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ._git_auth import authenticated_github_url, git_env


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


def check_is_node_pack(node_dir: Path, log=print) -> Optional[str]:
    """Is this directory a ComfyUI node pack? Returns an error string, or None.

    The predicate is `__init__.py`, and only that. It is derived from
    upstream's loader rather than guessed: `nodes.py` does
    `spec_from_file_location(name, module_path/"__init__.py")` for every
    directory under custom_nodes/, so a directory without one cannot load, on
    any layout. Zero false positives by construction.

    Deliberately NOT also accepting pyproject.toml / requirements.txt: those
    are evidence of *packaging*, not of nodehood. A directory with a stray
    pyproject and no __init__.py would pass such a gate and then fail
    identically several minutes later, which is a gate that buys nothing.

    A missing dependency file is a warning, not an error -- a pack with no
    dependencies is perfectly legal to ComfyUI.
    """
    if not (node_dir / "__init__.py").is_file():
        return (f"{node_dir} is not a ComfyUI node pack: no __init__.py.\n"
                f"ComfyUI loads a custom node by importing <pack>/__init__.py; "
                f"without it the directory cannot register any nodes.")
    if not (node_dir / "pyproject.toml").is_file() and \
       not (node_dir / "requirements.txt").is_file():
        log(f"[comfy-test] Note: {node_dir.name} has neither pyproject.toml nor "
            f"requirements.txt. That is legal, but it cannot be published to the "
            f"Comfy Registry as-is.")
    return None


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
    # Log the un-tokenised URL so any embedded PAT does not leak into CI logs.
    print(f"{log_prefix} clone {expanded} ({branch_desc}) -> {target}")
    fetch_url = authenticated_github_url(expanded)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([fetch_url, str(target)])
    # A network stall would otherwise hang until the CI job's own ceiling --
    # five hours on the hosted lanes, with no results.json and no explanation.
    # git_env() sets GIT_TERMINAL_PROMPT=0, which covers the credential-prompt
    # hang; this covers the silent one.
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=git_env(),
                           timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git clone of {expanded} timed out after 600s.\n"
            f"The remote stopped responding mid-transfer. Retry, or clone it "
            f"yourself and pass the local path instead.") from None
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed:\n{r.stderr}")

    # Validate what we just cloned. Without this, any repository on earth
    # clones successfully and the run proceeds to build an environment for it.
    problem = check_is_node_pack(target, log=lambda m: print(f"{log_prefix} {m}"))
    if problem:
        raise RuntimeError(problem)
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
    print(f"{log_prefix} LOCAL PATH -> copying {src_path} to {target}")
    shutil.copytree(src_path, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".venv", "venv", ".git",
                                                  "__pycache__", ".comfy-test"))
    return name
