"""Live contract: the assumptions we make about upstream still hold today.

Every other test in this suite is hermetic -- `test_torch_triple.py` stubs the
network out so it tests our resolution logic deterministically. That is the
right default, and it has one blind spot: **it cannot fail when upstream
changes shape.** If download.pytorch.org restyles its index pages tomorrow,
`_index_versions` quietly matches nothing, `newest_complete()` returns None,
and every green unit test stays green while the shipped tool cannot resolve a
torch version for anybody.

This file is the canary for that. It talks to the real network, so it is
**opt-in**: set `COMFY_TEST_LIVE=1`. It runs in CI before publishing to PyPI,
because shipping a release whose resolver is broken against the live index is
exactly the failure worth blocking.

## Unreachable is not the same as violated

A network blip must not block a release, and a genuine upstream change must.
So every check here separates the two: if we cannot reach a host, the test
**skips**; it fails only when we reached it and the answer was wrong. This is
the same distinction `torch_triple.resolve()` already makes between "not
published" and "index unreachable", for the same reason.

Run directly for a quick manual check:

    COMFY_TEST_LIVE=1 python -m pytest tests/test_live_contract.py -v
"""

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comfy_test.common import torch_triple as tt
# Deliberately only COMFYUI_REPO. How the clone is spelled is being reworked
# (clone --branch -> init+fetch+checkout); the URL is the part that is stable
# across both, and it is the part upstream can break under us.
from comfy_test.platforms.venv_server import COMFYUI_REPO

pytestmark = pytest.mark.skipif(
    os.environ.get("COMFY_TEST_LIVE") != "1",
    reason="live upstream test; set COMFY_TEST_LIVE=1 to run",
)

# The variants a run can actually install from. `_index_variant()` in config.py
# derives one of these from the active backend, so both must stay resolvable.
VARIANTS = ["cpu", "cu128"]


def _unreachable(exc: Exception) -> bool:
    """A transport failure, as opposed to upstream answering something wrong.

    A 404 is an *answer* -- the page we depend on is gone, which is a real
    contract break and must fail. A DNS or connection error is not.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError))


def _index_versions_or_skip(pkg: str, variant: str) -> set:
    try:
        return tt._index_versions(pkg, variant)
    except Exception as e:
        if _unreachable(e):
            pytest.skip(f"{variant} index unreachable: {type(e).__name__}: {e}")
        raise


# --------------------------------------------------------------------------
# torch: the wheel index still looks the way the parser expects
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant", VARIANTS)
def test_index_pages_still_parse(variant):
    """The canary. An empty match means the page changed, not that torch is gone.

    `_index_versions` scrapes filenames out of a PEP 503 page with a regex. If
    that regex stops matching, it returns an empty set and every downstream
    answer degrades to "no torch version available" -- silently, because an
    empty result is indistinguishable from a legitimately empty index.
    """
    for pkg in ("torch", "torchvision", "torchaudio"):
        found = _index_versions_or_skip(pkg, variant)
        assert found, (
            f"parsed ZERO versions of {pkg} from the {variant} index. "
            f"The index page format almost certainly changed -- "
            f"check the filename regex in torch_triple._index_versions."
        )
        # Deliberately not "contains 2.x": torchvision numbers its releases
        # 0.x, and encoding either series here would rot on the next bump.
        # What must hold is that we parsed *many* things and all of them are
        # plain versions -- a broken regex yields nothing or yields junk.
        assert len(found) >= 10, (
            f"{pkg} on {variant}: parsed only {sorted(found)}. The index "
            f"carries years of releases; this reads like a partial match."
        )
        for v in found:
            tt._sortable(v)  # ValueError if we scraped something that is not a version


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_default_torch_version_is_resolvable(variant):
    """`newest_complete()` backs the default pin. None means no run can start."""
    try:
        newest = tt.newest_complete(variant)
    except Exception as e:
        if _unreachable(e):
            pytest.skip(f"{variant} index unreachable: {e}")
        raise
    assert newest is not None, (
        f"no complete triple on the {variant} index. Either all three packages "
        f"stopped shipping together, or resolution is broken -- with no default "
        f"pin, every run that does not set torch_version explicitly fails."
    )


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_resolved_triple_actually_exists_on_that_index(variant):
    """The pin we emit must be installable, not merely well-formed.

    This is the check the hermetic tests structurally cannot make: they assert
    resolve() returns what the stub was told, never that those three files are
    downloadable. Emitting a pin that 404s at install time would surface as a
    broken run of the user's pack.
    """
    newest = tt.newest_complete(variant)
    if newest is None:
        pytest.skip(f"nothing complete on {variant} (covered by its own test)")

    torch_v, tv, ta = tt.resolve(newest, variant)

    for pkg, want in (("torch", torch_v), ("torchvision", tv), ("torchaudio", ta)):
        published = _index_versions_or_skip(pkg, variant)
        assert want in published, (
            f"resolve({newest!r}, {variant!r}) returned {pkg}=={want}, which is "
            f"NOT published on that index. This pin would fail at install."
        )


def test_pypi_still_declares_the_torch_pin_we_read():
    """torchvision's metadata is the one place the coupling is stated.

    `_TORCH_PIN` accepts both `torch==2.13.0` and the legacy `torch (==2.2.1)`.
    If upstream adopts a third spelling, or PyPI stops exposing requires_dist,
    the pin silently reads as None and torchvision drops out of every triple.
    """
    published = _index_versions_or_skip("torchvision", "cpu")
    newest_tv = max(published, key=tt._sortable)

    try:
        pin = tt._declared_torch_pin("torchvision", newest_tv)
    except Exception as e:
        if _unreachable(e):
            pytest.skip(f"PyPI unreachable: {e}")
        raise

    assert pin is not None, (
        f"torchvision {newest_tv} declares no parseable torch pin. Either PyPI "
        f"stopped serving requires_dist, or the metadata spelling changed -- "
        f"see torch_triple._TORCH_PIN."
    )
    assert pin.startswith("2."), f"implausible torch pin from torchvision: {pin!r}"


def test_the_shipped_default_config_resolves():
    """End-to-end of the path a user with no `torch_version` takes."""
    from comfy_test.common.config import _default_torch_version

    try:
        default = _default_torch_version()
    except Exception as e:
        if _unreachable(e):
            pytest.skip(f"index unreachable: {e}")
        raise

    assert default, (
        "the default torch_version resolved to empty -- a config that sets no "
        "torch_version would install an unpinned, unaligned triple."
    )
    tt.resolve(default, "cpu")  # raises TorchTripleError if incoherent


# --------------------------------------------------------------------------
# comfyui_version: the refs we tell users to write are fetchable
# --------------------------------------------------------------------------

def _ls_remote(*args, timeout=45):
    try:
        p = subprocess.run(["git", "ls-remote", *args],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        pytest.skip(f"git ls-remote unavailable: {e}")
    if p.returncode != 0 and ("Could not resolve host" in p.stderr
                              or "unable to access" in p.stderr):
        pytest.skip(f"github unreachable: {p.stderr.strip()[:120]}")
    return p


def test_the_comfyui_repo_url_still_resolves():
    """COMFYUI_REPO points at comfyanonymous/; the project moved to Comfy-Org/.

    GitHub redirects today, so every clone works. If that redirect is ever
    dropped, every fresh-install lane breaks at once -- and this is the only
    place that would notice before users do.
    """
    p = _ls_remote(COMFYUI_REPO, "HEAD")
    assert p.returncode == 0, (
        f"cannot resolve {COMFYUI_REPO}: {p.stderr.strip()[:200]}"
    )
    assert p.stdout.strip(), f"{COMFYUI_REPO} returned no HEAD"


def test_latest_resolves_to_a_real_default_branch():
    """`comfyui_version = "latest"` takes whatever the remote's default is.

    Asserted through HEAD rather than through the clone command, because the
    clone spelling is in flux and the remote's answer is the actual contract.
    """
    p = _ls_remote("--symref", COMFYUI_REPO, "HEAD")
    assert p.returncode == 0, f"cannot resolve HEAD: {p.stderr.strip()[:200]}"
    assert "refs/heads/" in p.stdout, (
        f"remote reports no default branch for HEAD:\n{p.stdout[:300]}"
    )


def test_a_real_release_tag_is_fetchable():
    """Tags are what `comfyui_version` pins in practice, so prove one resolves.

    Discovered rather than hardcoded: a literal tag in a test rots the moment
    upstream retags, and would fail for a reason that has nothing to do with us.
    """
    p = _ls_remote("--tags", COMFYUI_REPO)
    assert p.returncode == 0, f"cannot list tags: {p.stderr.strip()[:200]}"

    tags = [l.split("refs/tags/")[-1].replace("^{}", "")
            for l in p.stdout.splitlines() if "refs/tags/v" in l]
    assert tags, "ComfyUI publishes no v* tags -- docs tell users to pin one"

    newest = sorted(set(tags), key=lambda t: [
        int(x) if x.isdigit() else 0 for x in t.lstrip("v").split(".")])[-1]

    hit = _ls_remote(COMFYUI_REPO, newest)
    assert hit.stdout.strip(), (
        f"tag {newest} listed but not resolvable by name -- the exact ref a "
        f"pinned comfyui_version asks the remote for."
    )


def test_the_windows_portable_asset_still_exists():
    """The portable lane downloads a release asset by a hardcoded filename.

    A rename upstream breaks that lane only, and only at download time, deep
    into a run. HEAD is enough -- no need to pull the 7z itself.
    """
    from comfy_test.platforms.windows_portable.download import PORTABLE_LATEST_URL

    req = urllib.request.Request(PORTABLE_LATEST_URL, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        raise AssertionError(
            f"{PORTABLE_LATEST_URL} -> HTTP {e.code}. The portable release "
            f"asset was renamed or removed; the windows-portable lane is broken."
        ) from e
    except Exception as e:
        if _unreachable(e):
            pytest.skip(f"github unreachable: {e}")
        raise
    assert code == 200, f"unexpected status {code}"
