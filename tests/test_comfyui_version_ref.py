"""Guard: `comfyui_version` accepts every ref git can actually fetch.

A shallow `git clone --branch X` resolves a tag or a branch and nothing else,
so pinning ComfyUI to a commit SHA -- the only ref that identifies exactly
what was tested -- failed with a bare "Remote branch not found". The clone was
replaced with init+fetch+checkout, which resolves tags, branches and full SHAs
through one path.

An *abbreviated* SHA still cannot work: git expands abbreviations against
local objects and a fresh clone has none, so it is rejected at config-parse
time rather than twenty minutes into building a venv.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comfy_test.common.config_file import validate_comfyui_version
from comfy_test.common.errors import ConfigError
from comfy_test.platforms.venv_server import comfyui_clone_commands

FULL_SHA = "37ac9ff44ffd1e4cc4b481cee550ced67608ec3a"


def _fetch_ref(ref):
    """The ref argument the fetch command actually asks the remote for."""
    for cmd in comfyui_clone_commands(ref, "/tmp/x"):
        if "fetch" in cmd:
            return cmd[-1]
    raise AssertionError("no fetch command emitted")


def test_latest_asks_for_head():
    """"latest" must be translated to HEAD, never passed through to git.

    ComfyUI's remote carries a real tag literally named `latest`, pointing at
    a commit from 2023-05-15 ("Don't import custom nodes when the folder ends
    with .disabled"). `git fetch origin latest` resolves that tag, so passing
    the string through would silently test a three-year-old ComfyUI on every
    default run -- and it would look like it worked.
    """
    assert _fetch_ref("latest") == "HEAD"


def test_the_literal_string_latest_never_reaches_git():
    for cmd in comfyui_clone_commands("latest", "/tmp/x"):
        assert "latest" not in cmd, cmd


def test_a_sha_is_passed_through_verbatim():
    # The whole point: not rewritten, not prefixed with refs/heads/.
    assert _fetch_ref(FULL_SHA) == FULL_SHA


def test_tags_and_branches_go_through_the_same_path():
    assert _fetch_ref("v0.3.60") == "v0.3.60"
    assert _fetch_ref("master") == "master"


def test_no_branch_flag_survives():
    # `--branch` is what could not resolve a SHA; it must not come back.
    for ref in ("latest", "v0.3.60", FULL_SHA):
        for cmd in comfyui_clone_commands(ref, "/tmp/x"):
            assert "--branch" not in cmd, cmd


def test_fetch_is_still_shallow():
    for cmd in comfyui_clone_commands(FULL_SHA, "/tmp/x"):
        if "fetch" in cmd:
            assert "--depth" in cmd and cmd[cmd.index("--depth") + 1] == "1"


def test_full_sha_and_named_refs_are_accepted():
    for ok in ("latest", "v0.3.60", "master", "main", FULL_SHA):
        assert validate_comfyui_version(ok) == ok


def test_abbreviated_sha_is_rejected_with_a_useful_message():
    for short in ("37ac9ff", "37ac9ff44ffd1e4c", FULL_SHA[:39]):
        try:
            validate_comfyui_version(short)
        except ConfigError as e:
            assert "40" in str(e), f"message should name the fix: {e}"
        else:
            raise AssertionError(f"{short!r} should have been rejected")


def test_a_branch_that_happens_to_be_hex_is_not_mistaken_for_a_sha():
    # "abcdef" is 6 chars -- below the abbreviation window, so it stays a name.
    assert validate_comfyui_version("abcdef") == "abcdef"


def test_empty_is_rejected():
    for bad in ("", "   "):
        try:
            validate_comfyui_version(bad)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except Exception:
                failed += 1; print(f"FAIL {name}"); traceback.print_exc()
    sys.exit(1 if failed else 0)
