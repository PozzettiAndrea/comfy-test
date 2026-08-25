"""Guards for the config spellings we deliberately removed, and for the README.

## Why this file exists

Six tests used to fail because their fixtures still wrote `[test.platforms]`.
Rewriting those fixtures to `[test.lanes]` is the right fix -- and it removes
the last executable reference to the old spelling in the entire repo.

That matters more than it sounds. `config_file.py:255` raises a pointed
migration error for `[test.platforms]`, and *nothing asserted it*. Deleting the
whole branch (`if False:`) left the suite fully green. So the six red tests were
accidental coverage: they exercised the error by tripping over it, never by
asserting it. Rewrite them without replacing that, and a future reader finds an
error nobody can trip and removes it as dead code.

The rule this file encodes: **a change that removes the last coverage of a
behaviour must replace it in the same commit.**

## The README half

`pyproject.toml` ships `README.md` as the PyPI long description, so a config
example that the parser rejects is shipped documentation that cannot work. The
"Minimal Config" -- the first thing a new user copies -- was half-renamed to
`[test.lanes]` while keeping `platforms = [...]`, and was rejected outright.

Three doc sites went stale in one rename, which is a pattern rather than an
accident, so this asserts the examples parse rather than fixing three lines.
"""

import re
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from comfy_test.common.config_file import load_config
from comfy_test.common.errors import ConfigError


def _write(toml_text: str) -> Path:
    """A minimal pack on disk whose comfy-test.toml is `toml_text`."""
    d = Path(tempfile.mkdtemp())
    (d / "__init__.py").touch()
    cfg = d / "comfy-test.toml"
    cfg.write_text(toml_text, encoding="utf-8")
    return cfg


# --------------------------------------------------------------------------
# The removed spellings still fail, and still say what to write instead
# --------------------------------------------------------------------------

def test_platforms_section_still_raises_the_migration_error():
    """The coverage the fixture rewrite removed. Do not delete this.

    `[test.platforms]` was renamed to `[test.lanes]`. The old spelling is a
    hard error rather than an alias because the two are not the same shape --
    the old one took per-lane booleans, the new one takes an explicit
    allowlist, and silently coercing one into the other is exactly the guessing
    this config surface exists to refuse.
    """
    cfg = _write('[test]\n[test.platforms]\nlinux = true\n')
    with pytest.raises(ConfigError) as e:
        load_config(cfg)

    msg = str(e.value)
    assert "[test.platforms]" in msg, msg
    assert "[test.lanes]" in msg, msg      # names the replacement
    assert "lanes = [" in msg, msg         # and shows the new shape


def test_lanes_without_an_allowlist_is_rejected():
    """The old per-lane-boolean shape under the new section name.

    This is the mistake the rename invites: a user updates the section header,
    keeps the booleans, and would otherwise get a config where nothing runs.
    """
    cfg = _write('[test]\n[test.lanes]\nlinux = true\n')
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert "explicit allowlist" in str(e.value)


def test_bang_name_workflow_exclude_is_rejected():
    """`!name` looked like "run these, skip that" and never meant it.

    A single `!` entry flipped the whole list to *everything except*, dropped
    the includes, and ran every workflow on a CPU lane. There is now one way to
    exclude, and it is a table.
    """
    cfg = _write('[test]\n[test.lanes]\nlanes = ["linux-cpu"]\n'
                 '[test.workflows]\ncpu = ["basic", "!heavy"]\n')
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert "!name" in str(e.value)


def test_the_replacement_exclude_form_is_accepted():
    """The form the errors above tell people to use must actually work."""
    cfg = _write('[test]\n[test.lanes]\nlanes = ["linux-cpu"]\n'
                 '[test.workflows]\ncpu = { exclude = ["heavy"] }\n')
    load_config(cfg)  # raises if not


# --------------------------------------------------------------------------
# README examples are parsed by the parser they document
# --------------------------------------------------------------------------

def _readme_config_blocks():
    """Every ```toml block in README.md that is a whole comfy-test.toml.

    Fragment convention: a block is a full config iff it declares a `[test]`
    section. README also shows `comfy-env.toml` snippets and bare section
    excerpts, which are legitimately not loadable on their own -- feeding those
    to `load_config` would assert a falsehood.
    """
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    return [b for b in re.findall(r"```toml\n(.*?)```", text, re.S)
            if re.search(r"^\[test\]\s*$", b, re.M)]


def test_readme_ships_at_least_one_full_config():
    """Guards the guard: if the fence style changes, this must not pass vacuously."""
    assert _readme_config_blocks(), (
        "no ```toml block in README.md declares a [test] section. Either the "
        "examples were removed or the fragment convention above no longer "
        "matches -- do not let this test go quiet."
    )


def test_every_readme_config_example_actually_parses():
    """README is the PyPI long description. A rejected example ships broken.

    This caught the "Minimal Config" -- the copy-paste starting point -- sitting
    on PyPI as `[test.lanes]` with `platforms = [...]`, rejected by the very
    parser it was documenting.
    """
    for i, block in enumerate(_readme_config_blocks(), 1):
        try:
            load_config(_write(block))
        except ConfigError as e:
            raise AssertionError(
                f"README.md config example #{i} is rejected by load_config:\n"
                f"{e}\n\n--- the block ---\n{block}"
            ) from e
