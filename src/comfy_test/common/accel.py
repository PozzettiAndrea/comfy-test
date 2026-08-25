"""The one predicate for "is this run targeting CUDA?".

`COMFY_TEST_CUDA` is set to the **string** `"0"` on every CPU lane, not unset
(`_test-linux.yml`, `_test-macos.yml`, `_test-windows.yml`,
`_test-windows-portable.yml` and both desktop workflows). A non-empty string is
truthy, so `if os.environ.get("COMFY_TEST_CUDA")` is True on a CPU lane -- and
the codebase carried five different spellings of this question, two of them
wrong in exactly that way:

* `results.py` made every hosted lane's workflow timeout 24 hours
* `config.py` resolved the torch triple against the CUDA wheel index while the
  install pulled from the CPU one

Both were invisible because the two indexes happened to carry the same torch.
One predicate, one place, and a test (`tests/test_accel_predicate.py`) that
fails if a raw read reappears outside this module.
"""

from __future__ import annotations

import os

#: Values of `COMFY_TEST_CUDA` that mean "no". Everything else means yes.
_FALSEY = ("0", "", "false", "no")


def is_cuda_run() -> bool:
    """True when this run targets the CUDA accelerator.

    Reads `COMFY_TEST_CUDA`, treating `0`/empty/`false`/`no` (any case) as off.
    """
    return os.environ.get("COMFY_TEST_CUDA", "0").strip().lower() not in _FALSEY


def accel_name() -> str:
    """`cuda` or `cpu` -- the accelerator half of a lane id."""
    return "cuda" if is_cuda_run() else "cpu"
