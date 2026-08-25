"""Resolve a torch version to a coherent (torch, torchvision, torchaudio) triple.

The three ship compiled extensions linked against libtorch, and PyTorch
publishes no stable C++ ABI across releases -- so a mismatched trio installs
cleanly and dies at import with an undefined symbol. torchvision and torchaudio
declare this themselves: `torchvision 0.28.0` requires `torch==2.13.0`, exactly.

**The constraint is only visible from the dependents.** `torch` declares
nothing about its companions (its 17 deps are filelock, sympy, the nvidia-*
stack); the arrow runs vision/audio -> torch. So a triple is derived by reading
the dependents' metadata and inverting it, never by asking torch.

That inversion also makes *completeness* computable rather than assumed: a torch
version with a torchvision but no torchaudio is not installable as a triple, and
that is the normal state of affairs for a few weeks after each torch release,
because torchaudio trails.

Resolution order:
  1. TORCH_TRIPLES -- a checked-in cache. Offline, instant, no surprises.
  2. PyPI metadata -- so a torch released after this comfy-test still works
     without a code change.
  3. A hard error naming exactly what is missing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

_PYPI = "https://pypi.org/pypi/{pkg}/json"
_TIMEOUT = 15

# Populated on first network lookup: {torch_version: {"torchvision": v, "torchaudio": v}}
_derived_cache: Optional[Dict[str, Dict[str, str]]] = None


def _pypi(pkg: str) -> dict:
    with urllib.request.urlopen(_PYPI.format(pkg=pkg), timeout=_TIMEOUT) as r:
        return json.load(r)


def _torch_pin_of(pkg: str, version: str) -> Optional[str]:
    """The exact torch version `pkg==version` declares, if any."""
    try:
        info = _pypi(f"{pkg}/{version}")["info"]
    except Exception:
        return None
    for req in info.get("requires_dist") or []:
        if req.startswith("torch=="):
            return req.split("==", 1)[1].split(";")[0].strip()
    return None


def _release_versions(pkg: str, limit: int = 12) -> list:
    """Recent plain X.Y.Z releases of pkg, oldest first."""
    releases = _pypi(pkg)["releases"]
    plain = [v for v in releases if v.replace(".", "").isdigit()]
    plain.sort(key=lambda s: [int(x) for x in s.split(".")])
    return plain[-limit:]


def derive_triples(limit: int = 12) -> Dict[str, Dict[str, str]]:
    """Invert the dependents' metadata into {torch: {torchvision, torchaudio}}.

    Network. Cached for the process. Partial entries are kept deliberately --
    a torch with a torchvision but no torchaudio is exactly what the caller
    needs to be told about.
    """
    global _derived_cache
    if _derived_cache is not None:
        return _derived_cache

    out: Dict[str, Dict[str, str]] = {}
    reached = False
    errors = []
    for pkg in ("torchvision", "torchaudio"):
        try:
            versions = _release_versions(pkg, limit)
            reached = True
        except Exception as e:
            errors.append(f"{pkg}: {type(e).__name__}: {e}")
            continue
        for v in versions:
            pinned = _torch_pin_of(pkg, v)
            if pinned:
                out.setdefault(pinned, {})[pkg] = v

    # Distinguish "checked, and it is not there" from "could not check".
    # Without this an offline run reports an unknown version as nonexistent.
    if not reached:
        raise OSError("could not reach PyPI: " + "; ".join(errors))

    _derived_cache = out
    return out


class TorchTripleError(ValueError):
    """No coherent triple exists for the requested torch version."""


def _fmt_known(table: Dict[str, tuple]) -> str:
    keys = sorted(table, key=lambda s: [int(x) for x in s.split(".")], reverse=True)
    return ", ".join(keys)


def resolve(version: str, table: Dict[str, tuple]) -> Tuple[str, str, str]:
    """Resolve `version` to a full triple, or raise TorchTripleError.

    `table` is the checked-in TORCH_TRIPLES cache, consulted first.
    """
    if version in table:
        tv, ta = table[version]
        return (version, tv, ta)

    # Not cached -- ask PyPI, so a torch newer than this comfy-test still works.
    try:
        derived = derive_triples()
    except Exception as e:
        raise TorchTripleError(
            f"torch_version {version!r} is not a version comfy-test knows, and "
            f"PyPI could not be reached to look it up ({type(e).__name__}: {e}).\n\n"
            f"Known offline: {_fmt_known(table)}\n"
            f"Or state the triple explicitly:\n"
            f"    torch_version = \"{version}/<torchvision>/<torchaudio>\""
        ) from e

    found = derived.get(version, {})
    tv, ta = found.get("torchvision"), found.get("torchaudio")

    if tv and ta:
        return (version, tv, ta)

    # Partial: name precisely which companion is missing. This is the normal
    # state for a few weeks after a torch release -- torchaudio trails.
    if tv or ta:
        missing = "torchaudio" if tv else "torchvision"
        have = f"torchvision {tv}" if tv else f"torchaudio {ta}"
        complete = _newest_complete(derived, table)
        raise TorchTripleError(
            f"torch {version} has no complete triple yet: {have} exists, but no "
            f"matching {missing} has been published.\n\n"
            f"The three do not release together -- torch lands first and "
            f"{missing} follows, so a just-released torch is not installable as "
            f"a set.\n\n"
            f"Use the newest complete triple instead:\n"
            f"    torch_version = \"{complete}\"\n"
            f"or, if you know a {missing} that works, state it explicitly:\n"
            f"    torch_version = \"{version}/<torchvision>/<torchaudio>\""
        )

    complete = _newest_complete(derived, table)
    raise TorchTripleError(
        f"torch_version {version!r} does not exist, or publishes no "
        f"torchvision/torchaudio at all.\n\n"
        f"Known offline: {_fmt_known(table)}\n"
        f"Newest complete triple available: {complete}\n"
        f"Or state the triple explicitly:\n"
        f"    torch_version = \"{version}/<torchvision>/<torchaudio>\""
    )


def _newest_complete(derived: Dict[str, Dict[str, str]], table: Dict[str, tuple]) -> str:
    """Highest torch version with all three published. Falls back to the cache."""
    complete = [t for t, d in derived.items()
                if d.get("torchvision") and d.get("torchaudio")]
    complete += list(table)
    if not complete:
        return "unknown"
    return max(set(complete), key=lambda s: [int(x) for x in s.split(".")])


def newest_complete(table: Dict[str, tuple]) -> str:
    """The newest torch with all three published, preferring live data.

    Offline this is the top of the checked-in cache, which is why the cache is
    worth refreshing.
    """
    try:
        return _newest_complete(derive_triples(), table)
    except Exception:
        return _newest_complete({}, table)
