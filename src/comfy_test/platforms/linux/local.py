"""Local-specific behavior for Linux platform."""


def detect_gpu() -> bool:
    """Detect if an accelerator is available.

    Generic dispatcher -- the vendor probe (nvidia-smi for cuda) lives in the
    active backend, so no vendor call here.
    """
    from ...backends import active_backend
    return active_backend().hardware_name() is not None
