"""Resolve a torch version to a coherent (torch, torchvision, torchaudio) triple.

The three ship compiled extensions linked against libtorch and PyTorch publishes
no stable C++ ABI across releases, so a mismatched trio installs cleanly and
dies at import with an undefined symbol.

**Nothing is hand-maintained here.** The triple is derived from what PyPI
publishes, per torch version, and cached on disk. There is no table in source
to update when torch releases.

Two facts do the work, and neither is guessed:

1. **torchvision declares its torch exactly.** `torchvision 0.28.0` requires
   `torch==2.13.0`. Populated for every release, so the mapping is read, never
   computed from the version numbers.
2. **torchaudio shares torch's version number.** torch 2.11.0 <-> torchaudio
   2.11.0, verified across every release. This is a convention rather than
   metadata -- and it has to be, because torchaudio's metadata is *unreliable*:
   `torchaudio 2.10.0` declares `torch==2.10.0` but `torchaudio 2.11.0`
   declares nothing at all.

That inconsistency is why neither "let the resolver figure it out" nor "read
the dependents' metadata" is sufficient on its own. Asking uv for
`torch==2.13.0 torchvision torchaudio` silently yields torchaudio 2.11.0 --
no declared constraint to violate, and a broken import at runtime.

`torch` itself is never the source: it declares nothing about its companions
(its deps are filelock, sympy and the nvidia-* stack). The arrow runs
vision/audio -> torch.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

_PYPI = "https://pypi.org/pypi/{path}/json"
_TIMEOUT = 15
_CACHE_TTL = 24 * 3600

_memo: Optional[Dict[str, Dict[str, str]]] = None


def _cache_path() -> Path:
    return Path(os.environ.get("COMFY_TEST_HOME") or (Path.home() / ".comfy-test")) \
        / "torch_triples.json"


def _pypi(path: str) -> dict:
    with urllib.request.urlopen(_PYPI.format(path=path), timeout=_TIMEOUT) as r:
        return json.load(r)


def _plain_releases(pkg: str) -> list:
    """Published X.Y.Z versions of pkg, oldest first. Excludes pre-releases."""
    return sorted(
        (v for v in _pypi(pkg)["releases"] if v.replace(".", "").isdigit()),
        key=lambda s: [int(x) for x in s.split(".")],
    )


def _read_cache() -> Optional[Dict[str, Dict[str, str]]]:
    p = _cache_path()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - blob.get("fetched", 0) > _CACHE_TTL:
            return None
        return blob["triples"]
    except Exception:
        return None


def _write_cache(triples: Dict[str, Dict[str, str]]) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"fetched": time.time(), "triples": triples}),
                     encoding="utf-8")
    except Exception:
        pass  # a cache that cannot be written is not an error


def _stale_cache() -> Optional[Dict[str, Dict[str, str]]]:
    """The cache ignoring its TTL -- better than nothing when offline."""
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))["triples"]
    except Exception:
        return None


def triples(refresh: bool = False) -> Dict[str, Dict[str, str]]:
    """{torch_version: {"torchvision": v, "torchaudio": v}} from PyPI.

    Entries may be partial: a torch with a torchvision but no torchaudio is
    the normal state for a few weeks after each torch release, and the caller
    needs to be told that rather than have it hidden.
    """
    global _memo
    if _memo is not None and not refresh:
        return _memo
    if not refresh:
        cached = _read_cache()
        if cached is not None:
            _memo = cached
            return _memo

    out: Dict[str, Dict[str, str]] = {}

    # torchvision -> torch, read from the declared pin.
    for v in _plain_releases("torchvision"):
        try:
            info = _pypi(f"torchvision/{v}")["info"]
        except Exception:
            continue
        for req in info.get("requires_dist") or []:
            if req.startswith("torch=="):
                out.setdefault(req.split("==", 1)[1].split(";")[0].strip(),
                               {})["torchvision"] = v
                break

    # torchaudio: same version number as torch. Its metadata cannot be used --
    # 2.11.0 declares no torch dependency at all.
    audio = set(_plain_releases("torchaudio"))
    for t in set(out) | audio:
        if t in audio:
            out.setdefault(t, {})["torchaudio"] = t

    _memo = out
    _write_cache(out)
    return out


class TorchTripleError(ValueError):
    """No coherent triple exists for the requested torch version."""


def _sortable(v: str):
    try:
        return [int(x) for x in v.split(".")]
    except ValueError:
        return [0]


def newest_complete(table: Optional[dict] = None) -> str:
    """Highest torch version with both companions published."""
    try:
        known = triples()
    except Exception:
        known = _stale_cache() or {}
    complete = [t for t, d in known.items()
                if d.get("torchvision") and d.get("torchaudio")]
    return max(complete, key=_sortable) if complete else "unknown"


def resolve(version: str, table: Optional[dict] = None) -> Tuple[str, str, str]:
    """Resolve `version` to a full triple, or raise TorchTripleError.

    `table` is accepted and ignored -- kept so the older two-argument call
    signature does not break.
    """
    offline = False
    try:
        known = triples()
    except Exception as e:
        known = _stale_cache()
        offline = True
        if known is None:
            raise TorchTripleError(
                f"Cannot resolve torch {version}: PyPI is unreachable and no "
                f"cached data is available ({type(e).__name__}: {e}).\n\n"
                f"State the triple explicitly to proceed offline:\n"
                f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
            ) from e

    found = known.get(version, {})
    tv, ta = found.get("torchvision"), found.get("torchaudio")
    if tv and ta:
        return (version, tv, ta)

    where = " (from cached data; PyPI unreachable)" if offline else ""
    complete = max((t for t, d in known.items()
                    if d.get("torchvision") and d.get("torchaudio")),
                   key=_sortable, default="unknown")

    if tv or ta:
        missing = "torchaudio" if tv else "torchvision"
        have = f"torchvision {tv}" if tv else f"torchaudio {ta}"
        raise TorchTripleError(
            f"torch {version} has no complete triple yet{where}: {have} exists, "
            f"but no matching {missing} has been published.\n\n"
            f"The three do not release together -- torch lands first and "
            f"{missing} follows, so a just-released torch is not installable "
            f"as a set.\n\n"
            f"Use the newest complete triple instead:\n"
            f'    torch_version = "{complete}"\n'
            f"or state the triple explicitly if you know one that works:\n"
            f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
        )

    raise TorchTripleError(
        f"torch {version} publishes no torchvision or torchaudio{where}, or "
        f"does not exist.\n\n"
        f"Newest complete triple available: {complete}\n"
        f"Or state the triple explicitly:\n"
        f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
    )
