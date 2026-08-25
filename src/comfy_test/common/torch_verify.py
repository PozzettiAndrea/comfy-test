"""Prove the installed torch stack actually imports, before anything depends on it.

Resolving a coherent triple (`torch_triple.py`) makes the *pin* correct. It
cannot make the *installation* correct, and the gap between those two is where
this whole class of failure lives:

  - `torch==2.10.0` is satisfied by `2.10.0+cu128`, `2.10.0+cu130` and
    `2.10.0+cpu` alike, because PEP 440 ignores a local version segment unless
    the specifier names one. Three packages can each honour every declared
    constraint and still carry three different libtorch builds. Upstream sees
    this in the wild -- ComfyUI #14384: *"PyTorch has CUDA version 13.0 whereas
    TorchAudio has CUDA version 12.8"*.
  - torchaudio 2.11.0 declares **no** torch dependency at all, so a resolver
    has nothing to violate and will happily pair it with any torch.
  - Compiled extensions link `libtorch`, and PyTorch publishes no stable C++
    ABI across releases. The failure is `undefined symbol: _ZN3c10...` -- `c10`
    being torch's own namespace -- and it happens at **import**, not install.

So the install always succeeds. Without this check the first symptom is a level
failing much later, wearing the pack author's name, in a traceback that looks
like their bug and is not.

One `python -c` against the environment we just built answers it definitively,
and costs a subprocess.

## What counts as broken

Two things, and the second is the one a plain import can miss:

1. **A module does not import.** Fatal by definition.
2. **The three disagree on their local version tag** (`+cu128` vs `+cu130` vs
   `+cpu`). Sometimes this raises on import and sometimes it does not -- it
   depends on whether the mismatched pair share a symbol that gets touched
   during module init. Comparing the tags is deterministic where importing is
   not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Dict, Optional

from .errors import TestError

# Imported one at a time and reported individually: knowing *which* of the three
# failed is most of the diagnosis, and a single combined import statement throws
# that away. torch first, since a torch failure explains the other two.
_PROBE = r"""
import json
out, errs = {}, {}
for name in ("torch", "torchvision", "torchaudio"):
    try:
        out[name] = getattr(__import__(name), "__version__", "unknown")
    except BaseException as e:
        errs[name] = "%s: %s" % (type(e).__name__, e)
try:
    import torch
    out["_cuda"] = torch.version.cuda
except BaseException:
    out["_cuda"] = None
print("COMFY_TEST_TORCH_PROBE " + json.dumps({"versions": out, "errors": errs}))
"""

_TIMEOUT = 180


def _local_tag(version: str) -> str:
    """The `+cu128` part of `2.10.0+cu128`, or "" when there is none.

    An empty tag is not a mismatch on its own: PyPI's default build carries no
    local segment, so an all-default stack is legitimately all-empty.
    """
    return version.split("+", 1)[1] if "+" in version else ""


def verify_torch_stack(python: Path,
                       log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Import torch, torchvision and torchaudio in `python`. Raise if broken.

    Returns the three versions on success, so callers can record what actually
    got installed rather than what was asked for.
    """
    def _say(msg: str) -> None:
        if log:
            log(msg)

    try:
        proc = subprocess.run([str(python), "-c", _PROBE],
                              capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TestError(
            "torch stack check timed out",
            f"`import torch` did not finish within {_TIMEOUT}s in {python}. "
            f"That usually means a broken CUDA install stuck probing devices."
        ) from None
    except OSError as e:
        # Not a torch problem -- do not report it as one.
        _say(f"[torch-check] skipped: cannot run {python} ({e})")
        return {}

    line = next((l for l in proc.stdout.splitlines()
                 if l.startswith("COMFY_TEST_TORCH_PROBE ")), None)
    if line is None:
        raise TestError(
            "torch stack check produced no result",
            f"Ran `{python} -c <probe>` and got no marker line back.\n\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}"
        )

    data = json.loads(line.split(" ", 1)[1])
    versions = {k: v for k, v in data["versions"].items() if not k.startswith("_")}
    errors = data["errors"]
    cuda = data["versions"].get("_cuda")

    # --- 1. everything must import ---
    if errors:
        missing = ", ".join(sorted(errors))
        detail = "\n".join(f"    {k}: {v}" for k, v in sorted(errors.items()))
        ok = ", ".join(f"{k}=={v}" for k, v in sorted(versions.items())) or "none"
        raise TestError(
            f"the installed torch stack does not import ({missing})",
            f"Imported fine: {ok}\n"
            f"Failed:\n{detail}\n\n"
            f"torchvision and torchaudio ship compiled extensions linked "
            f"against libtorch, and PyTorch publishes no stable C++ ABI across "
            f"releases -- so a mismatched trio installs cleanly and dies here, "
            f"at import.\n\n"
            f"An `undefined symbol: _ZN3c10...` above is exactly that: c10 is "
            f"torch's own C++ namespace, so something was built against a "
            f"different torch than the one installed.\n\n"
            f"Pin the triple explicitly in comfy-test.toml:\n"
            f'    [test]\n    torch_version = "<torch>/<torchvision>/<torchaudio>"'
        )

    # --- 2. the three must agree on their build variant ---
    tags = {name: _local_tag(v) for name, v in versions.items()}
    if len(set(tags.values())) > 1:
        shown = "\n".join(f"    {k}=={versions[k]}" for k in sorted(versions))
        raise TestError(
            "the torch stack mixes build variants",
            f"All three satisfy their declared version pins and still cannot "
            f"work together:\n{shown}\n\n"
            f"`torch==X` ignores the local segment (+cu128 / +cu130 / +cpu) "
            f"unless the specifier names one, so a resolver cannot see this "
            f"disagreement -- but the compiled extensions link different "
            f"libtorch builds.\n\n"
            f"It usually means one of the three came from a different index. "
            f"Every wheel must come from the same one."
        )

    variant = next(iter(tags.values())) or "default (no local tag)"
    _say(f"[torch-check] ok: "
         + " ".join(f"{k}=={v}" for k, v in sorted(versions.items()))
         + f" | variant={variant} | torch.version.cuda={cuda}")
    return versions
