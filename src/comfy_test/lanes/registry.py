"""Canonical registry of the lanes comfy-test knows about.

Single source of truth for the supported-lane taxonomy. Everything else
derives from here:
  - common/config_file.py    -> valid `[test.lanes] lanes = [...]` tokens
  - common/config.py         -> get_lane_config() lookup
  - reporting/html_report.py -> the results-gallery LANES (id/label)
  - the CI job matrix        -> `comfy-test lanes --matrix-json`

A lane is an (os x backend x install_method) combination. `backend` names the accelerator
concretely -- cpu / cuda / rocm (rocm reserved, no runner wired yet); there is
no "gpu". Users select lanes by `id` (or an accepted alias) in comfy-test.toml.

Design rule (per review): store only irreducible facts; compute everything
derivable. Five stored fields; the rest are @property.
"""

from __future__ import annotations

from dataclasses import dataclass

# GitHub-hosted runner image per OS (linux is ubuntu, not "linux-latest").
_HOSTED_IMAGE = {"linux": "ubuntu-latest", "windows": "windows-latest", "macos": "macos-latest"}


@dataclass(frozen=True)
class Lane:
    # --- stored facts (the only things that can't be derived) ---
    id: str                 # canonical hyphenated id, e.g. "windows-cuda"
    os: str                 # "linux" | "windows" | "macos"
    backend: str            # "cpu" | "cuda" | "rocm"
    install_method: str     # "manual" | "portable" | "desktop"
    label: str              # human label for the results gallery

    # --- everything below is computed, never stored ---
    @property
    def config_key(self) -> str:
        """Underscore key used by TestConfig fields / get_lane_config.

        The cpu variant is the bare name (drop the redundant `-cpu` suffix):
        `linux-cpu`->`linux`, `windows-portable-cpu`->`windows_portable`,
        `macos-desktop`->`macos_desktop`. cuda variants keep their suffix:
        `windows-cuda`->`windows_cuda`, `windows-desktop-cuda`->`windows_desktop_cuda`.
        """
        key = self.id
        if self.backend == "cpu" and key.endswith("-cpu"):
            key = key[: -len("-cpu")]
        return key.replace("-", "_")

    @property
    def hosted(self) -> bool:
        """cpu lanes run on GitHub-hosted runners (test-matrix.yml);
        accelerator lanes are self-hosted / dispatch-test.yml."""
        return self.backend == "cpu"

    @property
    def is_dispatch_only(self) -> bool:
        return not self.hosted

    @property
    def cuda_capable(self) -> bool:
        """Apple Silicon is the only CUDA-less OS."""
        return self.os != "macos"

    @property
    def exe_suffix(self) -> str:
        return ".exe" if self.os == "windows" else ""

    @property
    def runner_labels(self):
        """`runs-on` for this lane: a hosted image string, or the self-hosted
        tag set for accelerator/desktop jobs."""
        if self.hosted:
            return _HOSTED_IMAGE[self.os]
        tags = ["self-hosted", self.os, self.backend]
        if self.install_method == "desktop":
            tags.append("vm")
        return tags

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every token accepted for this lane in `[test.lanes]`
        (id and config_key, in both hyphen and underscore spellings)."""
        toks = set()
        for base in {self.id, self.config_key}:
            toks.add(base)
            toks.add(base.replace("-", "_"))
            toks.add(base.replace("_", "-"))
        return tuple(sorted(toks))


# The full os x backend x install_method product comfy-test supports. (No macos-cuda:
# Apple Silicon has no CUDA. No linux-desktop / portable-desktop: not a thing.)
LANES: list[Lane] = [
    Lane("linux-cpu",             "linux",   "cpu",  "manual",      "Linux CPU"),
    Lane("linux-cuda",            "linux",   "cuda", "manual",      "Linux CUDA"),
    Lane("windows-cpu",           "windows", "cpu",  "manual",      "Windows CPU"),
    Lane("windows-cuda",          "windows", "cuda", "manual",      "Windows CUDA"),
    Lane("windows-portable-cpu",  "windows", "cpu",  "portable", "Win Portable CPU"),
    Lane("windows-portable-cuda", "windows", "cuda", "portable", "Win Portable CUDA"),
    Lane("macos-cpu",             "macos",   "cpu",  "manual",      "macOS CPU"),
    Lane("macos-desktop",         "macos",   "cpu",  "desktop",  "macOS Desktop"),
    Lane("windows-desktop",       "windows", "cpu",  "desktop",  "Windows Desktop"),
    Lane("windows-desktop-cuda",  "windows", "cuda", "desktop",  "Windows Desktop CUDA"),
]


def _validate() -> None:
    ids = [p.id for p in LANES]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate lane id in registry")
    alias_owner: dict[str, str] = {}
    for p in LANES:
        if p.backend not in ("cpu", "cuda", "rocm"):
            raise ValueError(f"{p.id}: bad backend {p.backend!r}")
        if p.install_method not in ("manual", "portable", "desktop"):
            raise ValueError(f"{p.id}: bad install_method {p.install_method!r}")
        for a in p.aliases:
            if a in alias_owner and alias_owner[a] != p.id:
                raise ValueError(f"alias {a!r} maps to both {alias_owner[a]} and {p.id}")
            alias_owner[a] = p.id


_validate()

# id -> Lane, and alias -> Lane (both hyphen and underscore forms).
BY_ID: dict[str, Lane] = {p.id: p for p in LANES}
_BY_ALIAS: dict[str, Lane] = {a: p for p in LANES for a in p.aliases}


def resolve(token: str) -> Lane | None:
    """Resolve an allowlist token (id or alias, hyphen or underscore) to a Lane."""
    return _BY_ALIAS.get(token) or _BY_ALIAS.get(token.replace("-", "_"))


def allowed_tokens() -> set[str]:
    """Every token accepted in `[test.lanes] lanes = [...]`."""
    return set(_BY_ALIAS)


def gallery_lanes() -> list[dict]:
    """The {id, label} rows the results gallery (html_report) renders."""
    return [{"id": p.id, "label": p.label} for p in LANES]


def matrix() -> dict:
    """Job matrix for the CI workflows: hosted lanes (GitHub runners) and
    dispatch-only lanes (self-hosted), each with its runs-on labels."""
    return {
        "hosted": [{"id": p.id, "config_key": p.config_key, "runs_on": p.runner_labels}
                   for p in LANES if p.hosted],
        "dispatch": [{"id": p.id, "config_key": p.config_key, "runs_on": p.runner_labels}
                     for p in LANES if p.is_dispatch_only],
    }
