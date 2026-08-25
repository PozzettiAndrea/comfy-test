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

TABLE = {
    "2.11.0": ("0.26.0", "2.11.0"),
    "2.10.0": ("0.25.0", "2.10.0"),
    "2.8.0": ("0.23.0", "2.8.0"),
}


def _stub(derived, raises=None):
    """Replace the network lookup for one test."""
    tt._derived_cache = None
    if raises is not None:
        def boom(limit=12):
            raise raises
        tt.derive_triples = boom
    else:
        tt.derive_triples = lambda limit=12: derived


def test_cached_version_resolves_without_network():
    _stub({}, raises=OSError("must not be called"))
    assert tt.resolve("2.10.0", TABLE) == ("2.10.0", "0.25.0", "2.10.0")


def test_derives_a_version_not_in_the_cache():
    _stub({"2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"}})
    assert tt.resolve("2.12.0", TABLE) == ("2.12.0", "0.27.0", "2.12.0")


def test_incomplete_triple_names_the_missing_package():
    """The normal state for weeks after a torch release: torchaudio trails."""
    _stub({"2.13.0": {"torchvision": "0.28.0"}})       # no torchaudio
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
        assert "could not be reached" in msg, msg
        assert "does not exist" not in msg, msg


def test_newest_complete_ignores_partial_entries():
    _stub({
        "2.12.0": {"torchvision": "0.27.0", "torchaudio": "2.12.0"},
        "2.13.0": {"torchvision": "0.28.0"},           # partial -- must not win
    })
    assert tt.newest_complete(TABLE) == "2.12.0"


def test_every_cached_entry_is_complete():
    """A cache entry missing a companion would resolve to an uninstallable set.

    Read via ast rather than import: `comfy_test.common.config` pulls in the
    package __init__ and with it the optional runtime deps, which this suite
    deliberately does not require.
    """
    import ast
    src = (_SRC / "common" / "config.py").read_text()
    table = None
    for node in ast.parse(src).body:
        targets = getattr(node, "targets", []) or ([getattr(node, "target", None)] if hasattr(node, "target") else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "TORCH_TRIPLES":
                table = ast.literal_eval(node.value)
    assert table, "TORCH_TRIPLES not found in config.py"
    for torch_v, pair in table.items():
        assert len(pair) == 2 and all(pair), f"{torch_v}: {pair}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
