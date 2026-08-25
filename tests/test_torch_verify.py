"""The post-install import check: what it catches, and what it must not flag.

No torch is installed to run these. `verify_torch_stack` shells out to a python
and reads one marker line back, so a fake interpreter -- a tiny script that
prints a canned probe result -- exercises the real parsing, the real comparison
and the real error text without a 2GB download.

That is the point of the marker-line protocol: it makes the check testable.

Runs under pytest or as a script.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comfy_test.common.errors import TestError
from comfy_test.common.torch_verify import _local_tag, verify_torch_stack


def _fake_python(tmp_path, versions, errors=None, cuda="12.8", name="fakepy"):
    """A stand-in interpreter that prints one canned probe result.

    Mimics the real contract exactly: ignore argv, emit the marker line.
    """
    payload = json.dumps({
        "versions": {**versions, "_cuda": cuda},
        "errors": errors or {},
    })
    p = tmp_path / name
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print('COMFY_TEST_TORCH_PROBE ' + {payload!r})\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# --------------------------------------------------------------------------
# the healthy case must stay quiet
# --------------------------------------------------------------------------

def test_a_coherent_cuda_stack_passes(tmp_path):
    py = _fake_python(tmp_path, {
        "torch": "2.11.0+cu128",
        "torchvision": "0.26.0+cu128",
        "torchaudio": "2.11.0+cu128",
    })
    got = verify_torch_stack(py)
    assert got["torch"] == "2.11.0+cu128"


def test_a_plain_pypi_stack_passes(tmp_path):
    """No local tag anywhere is legitimate -- PyPI's default build has none.

    Guards against the obvious wrong implementation: treating "" as a variant
    that disagrees with itself, which would fail every CPU-on-PyPI run.
    """
    py = _fake_python(tmp_path, {
        "torch": "2.11.0", "torchvision": "0.26.0", "torchaudio": "2.11.0",
    }, cuda=None)
    assert verify_torch_stack(py)["torchaudio"] == "2.11.0"


# --------------------------------------------------------------------------
# the two real failure modes
# --------------------------------------------------------------------------

def test_mixed_cuda_builds_are_caught_even_though_every_pin_is_satisfied(tmp_path):
    """Upstream ComfyUI #14384, reproduced as data.

    torch 2.12.0+cu130 with torchaudio 2.12.0+cu128: the public versions are
    identical, every declared pin is honoured, and it cannot work. No resolver
    can see this, because `torch==2.12.0` ignores the local segment.
    """
    py = _fake_python(tmp_path, {
        "torch": "2.12.0+cu130",
        "torchvision": "0.27.0+cu130",
        "torchaudio": "2.12.0+cu128",
    })
    with pytest.raises(TestError) as e:
        verify_torch_stack(py)
    msg = f"{e.value}"
    assert "mixes build variants" in msg, msg
    assert "cu130" in msg and "cu128" in msg, msg  # names both sides


def test_an_undefined_symbol_import_failure_is_caught(tmp_path):
    """The libtorch ABI break. Installs clean, dies at import.

    `c10` is torch's own C++ namespace, so this message is diagnostic and the
    error text must carry it through rather than swallowing it.
    """
    py = _fake_python(
        tmp_path,
        {"torch": "2.13.0", "torchvision": "0.28.0"},
        errors={"torchaudio": "ImportError: undefined symbol: _ZN3c104cuda9SetDeviceEa"},
    )
    with pytest.raises(TestError) as e:
        verify_torch_stack(py)
    msg = f"{e.value}"
    assert "torchaudio" in msg, msg
    assert "_ZN3c10" in msg, msg               # the actual symbol survives
    assert "torch==2.13.0" in msg, msg         # says what DID import


def test_the_failing_module_is_named_not_just_the_fact_of_failure(tmp_path):
    """Which of the three broke is most of the diagnosis."""
    py = _fake_python(
        tmp_path,
        {"torch": "2.11.0", "torchaudio": "2.11.0"},
        errors={"torchvision": "ModuleNotFoundError: No module named 'torchvision'"},
    )
    with pytest.raises(TestError) as e:
        verify_torch_stack(py)
    assert "torchvision" in f"{e.value}"


# --------------------------------------------------------------------------
# it must not manufacture failures of its own
# --------------------------------------------------------------------------

def test_an_unrunnable_interpreter_is_not_reported_as_a_torch_problem(tmp_path):
    """A missing python is an environment problem. Blaming torch for it would
    be a false red, and false reds are how gates get switched off."""
    assert verify_torch_stack(tmp_path / "does-not-exist") == {}


def test_a_probe_that_says_nothing_fails_loudly_rather_than_passing(tmp_path):
    """Silence must never read as success.

    If the probe cannot run -- a python that dies on startup, a wrapper that
    eats stdout -- returning {} would let a broken stack through wearing a
    green tick. The marker line is required, not optional.
    """
    py = tmp_path / "silent"
    py.write_text("#!/usr/bin/env python3\nprint('nothing useful')\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(TestError) as e:
        verify_torch_stack(py)
    assert "no result" in f"{e.value}"


def test_local_tag_parsing():
    assert _local_tag("2.10.0+cu128") == "cu128"
    assert _local_tag("2.10.0") == ""
    assert _local_tag("2.10.0+cpu") == "cpu"


# --------------------------------------------------------------------------
# the probe itself must be valid python on the interpreters we support
# --------------------------------------------------------------------------

def test_the_probe_runs_on_this_interpreter_and_emits_its_marker():
    """The probe is a string, so nothing else would catch a syntax error in it.

    Run against the *current* interpreter, which certainly has no torch --
    every import fails, and that is fine. What is asserted is that the probe
    executes and reports, rather than crashing.
    """
    from comfy_test.common.torch_verify import _PROBE
    proc = subprocess.run([sys.executable, "-c", _PROBE],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    line = next(l for l in proc.stdout.splitlines()
                if l.startswith("COMFY_TEST_TORCH_PROBE "))
    data = json.loads(line.split(" ", 1)[1])
    assert set(data) == {"versions", "errors"}


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                fn(Path(tempfile.mkdtemp()))
            else:
                fn()
            print(f"PASS {name}")
