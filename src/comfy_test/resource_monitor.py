"""Resource monitoring for workflow execution.

Tracks CPU, GPU, and RAM usage during workflow execution.
"""

import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class ResourceSample:
    """Single resource usage sample."""

    timestamp: float  # seconds since start
    cpu_cores: float  # effective cores used (e.g., 2.5 out of 10)
    ram_gb: float  # RAM used in GB
    gpu_percent: float | None = None  # None if no GPU or CPU-only test


class ResourceMonitor:
    """Background thread that samples CPU/GPU/RAM at regular intervals."""

    def __init__(self, interval: float = 1.0, monitor_gpu: bool = False):
        """Initialize resource monitor.

        Args:
            interval: Sampling interval in seconds (default: 1.0)
            monitor_gpu: Whether to monitor GPU usage (default: False)
        """
        self.interval = interval
        self.monitor_gpu = monitor_gpu
        self.samples: list[ResourceSample] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_time: float = 0

    def start(self):
        """Start monitoring in background thread."""
        self._stop_event.clear()
        self.samples = []
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        """Stop monitoring and return summary."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        return self.get_summary()

    def _monitor_loop(self):
        """Main monitoring loop - runs in background thread."""
        import psutil

        cpu_count = psutil.cpu_count() or 1

        while not self._stop_event.is_set():
            cpu_pct = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()

            sample = ResourceSample(
                timestamp=round(time.time() - self._start_time, 1),
                cpu_cores=round(cpu_pct * cpu_count / 100, 2),  # effective cores
                ram_gb=round(mem.used / (1024**3), 2),  # GB used
                gpu_percent=self._get_gpu_percent() if self.monitor_gpu else None,
            )
            self.samples.append(sample)
            self._stop_event.wait(self.interval)

    def _get_gpu_percent(self) -> float | None:
        """Get GPU utilization via nvidia-smi (Linux/Windows only)."""
        # Skip on macOS - no nvidia-smi
        if platform.system() == "Darwin":
            return None

        # NVIDIA GPU (Linux/Windows)
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return float(result.stdout.strip().split("\n")[0])
        except Exception:
            pass

        return None

    def get_summary(self) -> dict:
        """Return summary stats and timeline."""
        import psutil

        if not self.samples:
            return {}

        cpu_vals = [s.cpu_cores for s in self.samples]
        ram_vals = [s.ram_gb for s in self.samples]
        gpu_vals = [s.gpu_percent for s in self.samples if s.gpu_percent is not None]

        cpu_count = psutil.cpu_count() or 1
        total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)

        summary = {
            "cpu": {"peak": max(cpu_vals), "avg": round(sum(cpu_vals) / len(cpu_vals), 2)},
            "ram": {"peak": max(ram_vals), "avg": round(sum(ram_vals) / len(ram_vals), 2)},
            "cpu_count": cpu_count,
            "total_ram_gb": total_ram_gb,
            "samples": len(self.samples),
        }
        if gpu_vals:
            summary["gpu"] = {"peak": max(gpu_vals), "avg": round(sum(gpu_vals) / len(gpu_vals), 1)}

        # Timeline - all samples (cpu in cores, ram in GB)
        timeline = [
            {"t": s.timestamp, "cpu": s.cpu_cores, "ram": s.ram_gb, "gpu": s.gpu_percent}
            for s in self.samples
        ]
        summary["timeline"] = timeline

        return summary
