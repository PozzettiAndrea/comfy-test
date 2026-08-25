"""Resolve a torch version to a coherent (torch, torchvision, torchaudio) triple.

The three ship compiled extensions linked against libtorch and PyTorch publishes
no stable C++ ABI across releases, so a mismatched trio installs cleanly and
dies at import with an undefined symbol.

**Nothing is hand-maintained here** -- a table in source rots silently. The
triple is derived from the wheel index and cached on disk.

## Resolve against the index you install from

The decisive constraint: `torch==2.10.0` is satisfied by `2.10.0`,
`2.10.0+cu128`, `2.10.0+cu130` and `2.10.0+cpu` alike, because PEP 440 ignores
a local version segment unless the specifier names one. PyPI hosts only the
default build; the CUDA wheels live on `download.pytorch.org/whl/<variant>`.

So a triple that is "complete on PyPI" says nothing about the index the install
actually uses. Deriving from PyPI would have picked torch 2.12.0 the day
torchaudio 2.12.0 shipped, while the cu128 index still topped out at 2.11.0 --
and because the install passes `--extra-index-url pypi --index-strategy
unsafe-best-match`, uv would have quietly satisfied all three from plain PyPI
wheels on a CUDA lane. Exactly the fall-through the installer's own comment
warns about.

Reading the index's PEP 503 pages instead answers the question that matters,
and costs about a dozen requests rather than one per torchvision release ever
published.

## torchaudio: verify, never assume

torchaudio *usually* shares torch's version number, but not always:
`torchaudio 2.0.1` requires `torch==2.0.0` and `2.0.2` requires `2.0.1`. Its
metadata is also inconsistently populated -- `2.10.0` declares `torch==2.10.0`
while `2.11.0` declares nothing at all.

So: pair by version number, then **cross-check against the declared pin
whenever there is one**, and refuse rather than emit a pin known to conflict.
An unverifiable guess is fine; a guess contradicted by upstream is not.

`torch` itself is never a source -- it declares nothing about its companions.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

_PYPI = "https://pypi.org/pypi/{path}/json"
_INDEX = "https://download.pytorch.org/whl/{variant}/{pkg}/"
_TIMEOUT = 15
_CACHE_TTL = 24 * 3600

# Accepts both metadata spellings. Everything up to torchvision 0.17.1 and
# torchaudio 2.4.x uses the parenthesised legacy form, so `startswith("torch==")`
# silently loses two thirds of release history.
_TORCH_PIN = re.compile(r"^torch\s*\(?==\s*([0-9][^)\s;]*)\)?")

_memo: Dict[str, Dict[str, Dict[str, str]]] = {}


def _cache_path() -> Path:
    root = os.environ.get("COMFY_TEST_HOME") or (Path.home() / ".comfy-test")
    return Path(root) / "torch_triples.json"


def _index_versions(pkg: str, variant: str) -> set:
    """Plain X.Y.Z versions of pkg published on the wheel index for `variant`."""
    url = _INDEX.format(variant=variant, pkg=pkg)
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
        html = r.read().decode("utf-8", "replace")
    # Filenames look like torch-2.11.0%2Bcu128-cp312-...whl
    return set(re.findall(rf"{re.escape(pkg)}-(\d+\.\d+(?:\.\d+)?)(?:%2B|\+|-)", html))


def _declared_torch_pin(pkg: str, version: str) -> Optional[str]:
    """The torch version `pkg==version` declares, or None if it declares none.

    None is a real answer, not a failure: torchaudio 2.11.0 genuinely declares
    no torch dependency.
    """
    try:
        with urllib.request.urlopen(_PYPI.format(path=f"{pkg}/{version}"),
                                    timeout=_TIMEOUT) as r:
            info = json.load(r)["info"]
    except Exception:
        return None
    for req in info.get("requires_dist") or []:
        m = _TORCH_PIN.match(req.strip())
        if m:
            return m.group(1).split("+")[0]  # drop any +cu local tag
    return None


def _read_cache(variant: str) -> Optional[Dict[str, Dict[str, str]]]:
    try:
        blob = json.loads(_cache_path().read_text(encoding="utf-8"))
        entry = blob.get(variant)
        if entry and time.time() - entry.get("fetched", 0) <= _CACHE_TTL:
            return entry["triples"]
    except Exception:
        pass
    return None


def _stale_cache(variant: str) -> Optional[Dict[str, Dict[str, str]]]:
    """The cache ignoring its TTL -- better than nothing when offline."""
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))[variant]["triples"]
    except Exception:
        return None


def _write_cache(variant: str, triples: Dict[str, Dict[str, str]]) -> None:
    """Atomically, so parallel lanes on one runner cannot tear the file.

    A torn cache is worse than none: it also destroys the offline fallback.
    """
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = {}
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        blob[variant] = {"fetched": time.time(), "triples": triples}
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, p)
    except Exception:
        pass  # a cache that cannot be written is not an error


def triples(variant: str = "cpu", refresh: bool = False) -> Dict[str, Dict[str, str]]:
    """{torch: {"torchvision": v, "torchaudio": v}} for one wheel index.

    Entries may be partial -- a torch whose torchaudio has not shipped is the
    normal state for weeks after a release, and the caller must be told.
    """
    if not refresh:
        if variant in _memo:
            return _memo[variant]
        cached = _read_cache(variant)
        if cached is not None:
            _memo[variant] = cached
            return cached

    have = {pkg: _index_versions(pkg, variant)
            for pkg in ("torch", "torchvision", "torchaudio")}

    out: Dict[str, Dict[str, str]] = {t: {} for t in have["torch"]}

    # torchvision -> torch, from its declared pin. Only for versions the index
    # actually carries, so this is a handful of lookups, not the whole history.
    for v in sorted(have["torchvision"], key=_sortable, reverse=True)[:8]:
        pin = _declared_torch_pin("torchvision", v)
        if pin in out:
            out[pin]["torchvision"] = v

    # torchaudio: pair by version number here, VERIFY LAZILY in resolve().
    # Checking every candidate's declared pin during the build cost ~50 extra
    # requests to validate versions nobody asked for. The one that matters is
    # the one being resolved.
    for t in list(out):
        if t in have["torchaudio"]:
            out[t]["torchaudio"] = t

    _memo[variant] = out
    _write_cache(variant, out)
    return out


class TorchTripleError(ValueError):
    """No coherent triple exists for the requested torch version."""


def _sortable(v: str):
    parts = v.split(".")
    if not all(p.isdigit() for p in parts):
        raise ValueError(f"not a plain version: {v!r}")
    return [int(p) for p in parts]


def _complete(known: Dict[str, Dict[str, str]]) -> Optional[str]:
    done = [t for t, d in known.items()
            if d.get("torchvision") and d.get("torchaudio")]
    return max(done, key=_sortable) if done else None


def newest_complete(variant: str = "cpu") -> Optional[str]:
    """Highest torch on this index with both companions. None if undeterminable.

    None rather than a sentinel string: a caller must not be able to feed
    "unknown" back into resolve().
    """
    try:
        known = triples(variant)
    except Exception:
        known = _stale_cache(variant) or {}
    return _complete(known)


def resolve(version: str, variant: str = "cpu") -> Tuple[str, str, str]:
    """Resolve `version` to a full triple on `variant`, or raise."""
    offline = False
    try:
        known = triples(variant)
    except Exception as e:
        known = _stale_cache(variant)
        offline = True
        if known is None:
            raise TorchTripleError(
                f"Cannot resolve torch {version}: the {variant} wheel index is "
                f"unreachable and nothing is cached ({type(e).__name__}: {e}).\n\n"
                f"State the triple explicitly to proceed offline:\n"
                f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
            ) from e

    found = known.get(version, {})
    tv, ta = found.get("torchvision"), found.get("torchaudio")

    # Verify the version-number pairing against upstream's own declaration
    # before handing it back. torchaudio 2.0.1 requires torch==2.0.0, so
    # equality is a good guess and a bad guarantee. A missing declaration is
    # fine (2.11.0 has none); a contradicting one is not.
    if ta and not offline:
        declared = _declared_torch_pin("torchaudio", ta)
        if declared is not None and declared != version:
            raise TorchTripleError(
                f"torchaudio {ta} declares torch=={declared}, not {version}.\n\n"
                f"Pairing them would conflict on install. torchaudio usually "
                f"shares torch's version number, but not always -- 2.0.1 "
                f"requires torch 2.0.0.\n\n"
                f"State the triple explicitly if you know a torchaudio that "
                f"works:\n"
                f'    torch_version = "{version}/{tv or "<torchvision>"}/<torchaudio>"'
            )

    if tv and ta:
        return (version, tv, ta)

    where = f" on the {variant} index"
    if offline:
        where += " (cached; index unreachable)"
    best = _complete(known)
    suggestion = (f"Use the newest complete triple instead:\n"
                  f'    torch_version = "{best}"\n' if best else "")

    if tv or ta:
        missing = "torchaudio" if tv else "torchvision"
        have = f"torchvision {tv}" if tv else f"torchaudio {ta}"
        raise TorchTripleError(
            f"torch {version} has no complete triple{where}: {have} is "
            f"published, but no usable {missing} is.\n\n"
            f"Either it has not shipped yet -- the three do not release "
            f"together -- or the {missing} of that version declares a "
            f"different torch, which would conflict on install.\n\n"
            f"{suggestion}"
            f"or state the triple explicitly:\n"
            f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
        )

    raise TorchTripleError(
        f"torch {version} is not published{where}, or ships neither companion."
        f"\n\n{suggestion}"
        f"Or state the triple explicitly:\n"
        f'    torch_version = "{version}/<torchvision>/<torchaudio>"'
    )
