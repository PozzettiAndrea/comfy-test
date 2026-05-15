"""Shared git auth helpers for comfy-test.

Both the test path (clones the node + its dependent nodes) and the publish path
(pushes to gh-pages) need to authenticate against github.com when a token is
available in the environment. Centralising here so there's one URL-rewrite and
one env-builder, and so private node repos work on every CI lane that already
forwards a `NODE_PAT` / `GH_TOKEN` / `GITHUB_TOKEN` secret.
"""

from __future__ import annotations

import os
import re


_GH_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?:[^@/]*@)?github\.com/(?P<path>.+?)/?$",
    re.IGNORECASE,
)


def _token_from_env() -> str | None:
    return (
        os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("NODE_PAT")
        or None
    )


def authenticated_github_url(url_or_repo: str) -> str:
    """Return an HTTPS github.com URL, with embedded `x-access-token:<token>` auth
    when a token is set in env. Otherwise returns the plain URL.

    Accepts either:
      - 'owner/repo'                          -> 'https://github.com/owner/repo.git'
      - 'https://github.com/owner/repo'       -> '.../owner/repo.git'
      - 'https://github.com/owner/repo.git'   -> unchanged path
      - any non-github URL (or already-tokenised github URL) -> returned untouched.

    The `.git` suffix is preserved exactly as given; we only add one when
    expanding the bare `owner/repo` shorthand (to match what users type).
    """
    s = url_or_repo.strip()

    # owner/repo shorthand: expand to canonical .git URL.
    if "://" not in s and s.count("/") == 1 and not os.path.exists(s):
        repo = s
        token = _token_from_env()
        if token:
            return f"https://x-access-token:{token}@github.com/{repo}.git"
        return f"https://github.com/{repo}.git"

    m = _GH_URL_RE.match(s)
    if not m:
        return s  # non-github (gitlab, self-hosted, ssh, ...) or already has user:pass
    token = _token_from_env()
    if not token:
        return s
    return f"https://x-access-token:{token}@github.com/{m.group('path')}"


def git_env() -> dict:
    """Env for git subprocesses: never block on a credential prompt.

    Without this, an unauthenticated clone of a private repo hangs Windows git
    on the credential helper UI dialog or stdin prompt; with it, git fails
    immediately with a clean 'fatal: could not read Username' error.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
