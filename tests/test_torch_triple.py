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

TABLE = None  # there is no table -- everything is derived


def _stub(derived, raises=None):
    """Replace the PyPI lookup for one test. No network, no disk cache."""
    tt._memo = None
    tt._stale_cache = lambda: None
    if raises is not None:
        def boom(refresh=False):
            raise raises
        tt.triples = boom
    else:
        tt.triples = lambda refresh=False: derived


def test_resolves_a_complete_triple():
    _stub({"2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"}})
    assert tt.resolve("2.12.0", TABLE) == ("2.12.0", "0.27.0", "2.12.0")


def test_unreachable_with_no_cache_says_so():
    """Must not claim a version does not exist when we simply could not look."""
    _stub({}, raises=OSError("network unreachable"))
    try:
        tt.resolve("2.12.0", TABLE)
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "unreachable" in msg, msg
        assert "does not exist" not in msg, msg


def test_incomplete_triple_names_the_missing_package():
    """The normal state for weeks after a torch release: torchaudio trails."""
    _stub({
        "2.11.0": {"torchvision": "0.26.0", "torchaudio": "2.11.0"},  # complete
        "2.13.0": {"torchvision": "0.28.0"},                          # no torchaudio
    })
    try:
        tt.resolve("2.13.0", TABLE)
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
        tt.resolve("9.9.9", TABLE)
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        assert "does not exist" in str(e)

    _stub({}, raises=OSError("network unreachable"))
    try:
        tt.resolve("9.9.9", TABLE)
        raise AssertionError("expected TorchTripleError")
    except tt.TorchTripleError as e:
        msg = str(e)
        assert "unreachable" in msg, msg
        assert "does not exist" not in msg, msg


def test_newest_complete_ignores_partial_entries():
    _stub({
        "2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"},
        "2.13.0": {"torchvision": "0.28.0"},           # partial -- must not win
    })
    assert tt.newest_complete(TABLE) == "2.12.0"


def test_no_hand_maintained_table_reappears():
    """The whole point: nothing in source lists torch versions.

    A table is maintenance that silently rots -- the previous one pinned a
    torch one release older than it already knew about. If someone reintroduces
    one, this fails.
    """
    src = (_SRC / "common" / "config.py").read_text()
    for banned in ("TORCH_TRIPLES", "DEFAULT_TORCH_VERSION ="):
        assert banned not in src, f"{banned} is back in config.py"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
