"""Triple resolution: cache hits, the slash escape hatch, and how it fails.

No network. Live PyPI lookups are stubbed, so these run offline and
deterministically -- the point is the resolution logic and the error text, not
what PyPI happens to publish today.

Runs under pytest or as a script (`python tests/test_torch_triple.py`).
"""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "comfy_test"

# Load the module by path. `import comfy_test.common.torch_triple` would run the
# package __init__, which pulls in the optional runtime deps (websocket,
# playwright...). torch_triple itself is stdlib-only, so this suite needs none
# of them and stays runnable on a bare checkout.
_spec = importlib.util.spec_from_file_location(
    "_tt_under_test", _SRC / "common" / "torch_triple.py")
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)

def _stub(derived, raises=None, audio_pin=None):
    """Replace the index/PyPI lookups for one test. No network, no disk cache.

    `audio_pin` is what torchaudio claims to require, for the cross-check.
    """
    tt._memo = {}
    tt._stale_cache = lambda variant="cpu": None
    tt._declared_torch_pin = lambda pkg, v: audio_pin
    if raises is not None:
        def boom(variant="cpu", refresh=False):
            raise raises
        tt.triples = boom
    else:
        tt.triples = lambda variant="cpu", refresh=False: derived


def test_resolves_a_complete_triple():
    _stub({"2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"}})
    assert tt.resolve("2.12.0") == ("2.12.0", "0.27.0", "2.12.0")


def test_unreachable_with_no_cache_says_so():
    """Must not claim a version does not exist when we simply could not look."""
    _stub({}, raises=OSError("network unreachable"))
    try:
        tt.resolve("2.12.0")
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "unreachable" in msg, msg
        assert "not published" not in msg, msg


def test_incomplete_triple_names_the_missing_package():
    """The normal state for weeks after a torch release: torchaudio trails."""
    _stub({
        "2.11.0": {"torchvision": "0.26.0", "torchaudio": "2.11.0"},  # complete
        "2.13.0": {"torchvision": "0.28.0"},                          # no torchaudio
    })
    try:
        tt.resolve("2.13.0")
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "torchaudio" in msg, msg
        assert "0.28.0" in msg, msg           # says what DOES exist
        assert "2.11.0" in msg, msg           # suggests the newest complete one
        assert "<torchvision>" in msg, msg    # offers the escape hatch


def test_unknown_version_is_distinguished_from_unreachable():
    """'Checked, not there' and 'could not check' must not read the same."""
    _stub({"2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"}})
    try:
        tt.resolve("9.9.9")
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        assert "not published" in str(e)

    _stub({}, raises=OSError("network unreachable"))
    try:
        tt.resolve("9.9.9")
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "unreachable" in msg, msg
        assert "not published" not in msg, msg


def test_newest_complete_ignores_partial_entries():
    _stub({
        "2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"},
        "2.13.0": {"torchvision": "0.28.0"},           # partial -- must not win
    })
    assert tt.newest_complete() == "2.12.0"


def test_no_hand_maintained_table_reappears():
    """The whole point: nothing in source lists torch versions.

    A table is maintenance that silently rots -- the previous one pinned a
    torch one release older than it already knew about. If someone reintroduces
    one, this fails.
    """
    src = (_SRC / "common" / "config.py").read_text()
    for banned in ("TORCH_TRIPLES", "DEFAULT_TORCH_VERSION ="):
        assert banned not in src, f"{banned} is back in config.py"




def test_refuses_a_torchaudio_that_declares_a_different_torch():
    """torchaudio 2.0.1 requires torch==2.0.0 -- equality is a guess, not a law.

    Pairing by version number and shipping it unverified would emit a pin that
    conflicts on install.
    """
    _stub({"2.0.1": {"torchvision": "0.15.2", "torchaudio": "2.0.1"}},
          audio_pin="2.0.0")
    try:
        tt.resolve("2.0.1")
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "2.0.0" in msg and "not 2.0.1" in msg, msg


def test_accepts_a_torchaudio_that_declares_nothing():
    """torchaudio 2.11.0 declares no torch dep at all -- that is not a failure."""
    _stub({"2.11.0": {"torchvision": "0.26.0", "torchaudio": "2.11.0"}},
          audio_pin=None)
    assert tt.resolve("2.11.0") == ("2.11.0", "0.26.0", "2.11.0")


def test_legacy_parenthesised_metadata_is_parsed():
    """Everything up to torchvision 0.17.1 spells it `torch (==2.2.1)`."""
    assert tt._TORCH_PIN.match("torch (==2.1.0)").group(1) == "2.1.0"
    assert tt._TORCH_PIN.match("torch==2.13.0").group(1) == "2.13.0"
    assert tt._TORCH_PIN.match("torch (==2.0.0+cu117)").group(1).split("+")[0] == "2.0.0"
    assert tt._TORCH_PIN.match("torchvision==0.1") is None


def test_version_sort_rejects_garbage():
    """`"2..0".replace(".","").isdigit()` was True and then exploded downstream."""
    for bad in ("2..0", "2.0.0rc1", ""):
        try:
            tt._sortable(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
