"""Canonical registry of the platforms comfy-test knows about.

Single source of truth for the supported-platform taxonomy. Everything else
derives from here:
  - common/config_file.py    -> valid `[test.platforms] platforms = [...]` tokens
  - common/config.py         -> get_platform_config() lookup
  - reporting/html_report.py -> the results-gallery PLATFORMS (id/label)
  - the CI job matrix        -> `comfy-test platforms --matrix-json`

A platform is an (os x backend x kind) target. `backend` names the accelerator
concretely -- cpu / cuda / rocm (rocm reserved, no runner wired yet); there is
no "gpu". Users select targets by `id` (or an accepted alias) in comfy-test.toml.

Design rule (per review): store only irreducible facts; compute everything
derivable. Five stored fields; the rest are @property.
"""

from __future__ import annotations

from dataclasses import dataclass

# GitHub-hosted runner image per OS (linux is ubuntu, not "linux-latest").
_HOSTED_IMAGE = {"linux": "ubuntu-latest", "windows": "windows-latest", "macos": "macos-latest"}


@dataclass(frozen=True)
class Platform:
    # --- stored facts (the only things that can't be derived) ---
    id: str                 # canonical hyphenated id, e.g. "windows-cuda"
    os: str                 # "linux" | "windows" | "macos"
    backend: str            # "cpu" | "cuda" | "rocm"
    kind: str               # "server" | "portable" | "desktop"
    label: str              # human label for the results gallery

    # --- everything below is computed, never stored ---
    @property
    def config_key(self) -> str:
        """Underscore key used by TestConfig fields / get_platform_config.

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
        """cpu platforms run on GitHub-hosted runners (test-matrix.yml);
        accelerator platforms are self-hosted / dispatch-test.yml."""
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
    def venv_bindir(self) -> str | None:
        """Subdir holding the venv python. None for kinds with no venv of their
        own (portable uses ComfyUI's embedded python; desktop is the Electron app)."""
        if self.kind != "server":
            return None
        return "Scripts" if self.os == "windows" else "bin"

    @property
    def runner_labels(self):
        """`runs-on` for this platform: a hosted image string, or the self-hosted
        tag set for accelerator/desktop jobs."""
        if self.hosted:
            return _HOSTED_IMAGE[self.os]
        tags = ["self-hosted", self.os, self.backend]
        if self.kind == "desktop":
            tags.append("vm")
        return tags

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every token accepted for this platform in `[test.platforms]`
        (id and config_key, in both hyphen and underscore spellings)."""
        toks = set()
        for base in {self.id, self.config_key}:
            toks.add(base)
            toks.add(base.replace("-", "_"))
            toks.add(base.replace("_", "-"))
        return tuple(sorted(toks))


# The full os x backend x kind product comfy-test supports. (No macos-cuda:
# Apple Silicon has no CUDA. No linux-desktop / portable-desktop: not a thing.)
PLATFORMS: list[Platform] = [
    Platform("linux-cpu",             "linux",   "cpu",  "server",   "Linux CPU"),
    Platform("linux-cuda",            "linux",   "cuda", "server",   "Linux CUDA"),
    Platform("windows-cpu",           "windows", "cpu",  "server",   "Windows CPU"),
    Platform("windows-cuda",          "windows", "cuda", "server",   "Windows CUDA"),
    Platform("windows-portable-cpu",  "windows", "cpu",  "portable", "Win Portable CPU"),
    Platform("windows-portable-cuda", "windows", "cuda", "portable", "Win Portable CUDA"),
    Platform("macos-cpu",             "macos",   "cpu",  "server",   "macOS CPU"),
    Platform("macos-desktop",         "macos",   "cpu",  "desktop",  "macOS Desktop"),
    Platform("windows-desktop",       "windows", "cpu",  "desktop",  "Windows Desktop"),
    Platform("windows-desktop-cuda",  "windows", "cuda", "desktop",  "Windows Desktop CUDA"),
]


def _validate() -> None:
    ids = [p.id for p in PLATFORMS]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate platform id in registry")
    alias_owner: dict[str, str] = {}
    for p in PLATFORMS:
        if p.backend not in ("cpu", "cuda", "rocm"):
            raise ValueError(f"{p.id}: bad backend {p.backend!r}")
        if p.kind not in ("server", "portable", "desktop"):
            raise ValueError(f"{p.id}: bad kind {p.kind!r}")
        for a in p.aliases:
            if a in alias_owner and alias_owner[a] != p.id:
                raise ValueError(f"alias {a!r} maps to both {alias_owner[a]} and {p.id}")
            alias_owner[a] = p.id


_validate()

# id -> Platform, and alias -> Platform (both hyphen and underscore forms).
BY_ID: dict[str, Platform] = {p.id: p for p in PLATFORMS}
_BY_ALIAS: dict[str, Platform] = {a: p for p in PLATFORMS for a in p.aliases}


def resolve(token: str) -> Platform | None:
    """Resolve an allowlist token (id or alias, hyphen or underscore) to a Platform."""
    return _BY_ALIAS.get(token) or _BY_ALIAS.get(token.replace("-", "_"))


def allowed_tokens() -> set[str]:
    """Every token accepted in `[test.platforms] platforms = [...]`."""
    return set(_BY_ALIAS)


def gallery_platforms() -> list[dict]:
    """The {id, label} rows the results gallery (html_report) renders."""
    return [{"id": p.id, "label": p.label} for p in PLATFORMS]


def matrix() -> dict:
    """Job matrix for the CI workflows: hosted platforms (GitHub runners) and
    dispatch-only platforms (self-hosted), each with its runs-on labels."""
    return {
        "hosted": [{"id": p.id, "config_key": p.config_key, "runs_on": p.runner_labels}
                   for p in PLATFORMS if p.hosted],
        "dispatch": [{"id": p.id, "config_key": p.config_key, "runs_on": p.runner_labels}
                     for p in PLATFORMS if p.is_dispatch_only],
    }
