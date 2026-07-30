"""CUDA backend.

The ONLY place in comfy-test that runs `nvidia-smi` or names the CUDA torch
index. Generic callers (results.py, the VRAM/resource monitors, the platform
torch-index selection) dispatch via ``backends.active_backend()`` and contain no
vendor call themselves. Adding ROCm is a sibling ``RocmBackend`` (rocm-smi)
registered in ``backends/__init__.py`` -- not an edit to any generic caller.

Each method reproduces its former caller's exact query/timeout/return semantics
so extraction is behaviour-preserving.
"""

from __future__ import annotations

import subprocess

CUDA_TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


def _smi(args: list[str], timeout: int) -> str | None:
    """Run nvidia-smi with args; return stdout on success, None otherwise."""
    try:
        r = subprocess.run(
            ["nvidia-smi", *args], capture_output=True, text=True, timeout=timeout
        )
        return r.stdout if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


class CudaBackend:
    name = "cuda"

    # --- torch index (was platforms/*/platform.py PYTORCH_CUDA_INDEX) ---------
    def torch_index(self) -> str:
        return CUDA_TORCH_INDEX

    # --- presence / identity (was results.has_cuda / get_hardware_info) --------
    def accelerator_present(self) -> bool:
        try:
            return subprocess.run(["nvidia-smi"], capture_output=True, timeout=10).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def hardware_names(self) -> list[str]:
        out = _smi(["--query-gpu=name", "--format=csv,noheader"], timeout=10)
        if not out:
            return []
        return [g.strip() for g in out.strip().split("\n") if g.strip()]

    def hardware_name(self) -> str | None:
        names = self.hardware_names()
        return names[0] if names else None

    # --- VRAM (was common/vram_monitor.py; timeout 10, 0/{} on failure) -------
    def total_vram_mib(self) -> int:
        out = _smi(["--query-gpu=memory.total", "--format=csv,noheader,nounits"], timeout=10)
        if not out:
            return 0
        try:
            return int(out.strip().split("\n")[0].strip())
        except (ValueError, IndexError):
            return 0

    def vram_per_pid_mib(self) -> dict[int, int]:
        out = _smi(
            ["--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            timeout=10,
        )
        if not out:
            return {}
        vram: dict[int, int] = {}
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    vram[int(parts[0])] = int(parts[1])
                except ValueError:
                    pass
        return vram

    # --- VRAM (was common/resource_monitor.py; timeout 2, None on failure) ----
    def system_vram_used_mib(self) -> float | None:
        out = _smi(["--query-gpu=memory.used", "--format=csv,noheader,nounits"], timeout=2)
        if out is None:
            return None
        try:
            return float(out.strip().split("\n")[0])
        except (ValueError, IndexError):
            return None

    def vram_used_by_pid_mib(self) -> dict[int, float] | None:
        """pid -> used MiB from the compute-apps query. None on query failure."""
        out = _smi(
            ["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            timeout=2,
        )
        if out is None:
            return None
        result: dict[int, float] = {}
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    result[int(parts[0].strip())] = float(parts[1].strip())
                except ValueError:
                    pass
        return result
