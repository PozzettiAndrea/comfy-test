"""Accelerator backend abstraction for comfy-test.

Every accelerator-specific call (nvidia-smi, the CUDA torch index) lives in a
backend implementation named for its vendor (`CudaBackend`). Generic callers use
`active_backend()` and never contain a vendor call, so adding ROCm = a
`RocmBackend` registered here, not edits to generic code.

The active backend is chosen by `COMFY_TEST_BACKEND` (unset => cuda) -- the same
signal `cli/run.py` uses to mint the platform bucket.
"""

from __future__ import annotations

import os

from .cuda import CudaBackend

# backend name -> implementation. Adding a backend = one entry here.
_REGISTRY = {
    "cuda": CudaBackend,
}


def active_backend_name() -> str:
    """The backend this run targets. `cpu` has no accelerator backend object."""
    return os.environ.get("COMFY_TEST_BACKEND") or "cuda"


def active_backend():
    """The accelerator backend for this run (raises if unknown/unregistered)."""
    name = active_backend_name()
    impl = _REGISTRY.get(name)
    if impl is None:
        raise ValueError(
            f"no accelerator backend registered for COMFY_TEST_BACKEND={name!r}; "
            f"known: {sorted(_REGISTRY)}"
        )
    return impl()


__all__ = ["active_backend", "active_backend_name", "CudaBackend"]
