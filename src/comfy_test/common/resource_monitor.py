"""Resource monitoring for workflow execution.

Tracks RAM and VRAM usage during workflow execution.
"""

import platform
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class ResourceSample:
    """Single resource usage sample."""

    timestamp: float  # seconds since start
    ram_gb: float  # RAM used in GB
    vram_gb: float | None = None  # VRAM used in GB, None if no GPU


class ResourceMonitor:
    """Background thread that samples CPU/GPU/RAM at regular intervals."""

    def __init__(self, interval: float = 1.0, monitor_gpu: bool = False, pid: int | None = None):
        """Initialize resource monitor.

        Args:
            interval: Sampling interval in seconds (default: 1.0)
            monitor_gpu: Whether to monitor GPU usage (default: False)
            pid: Process ID to track RAM for. If set, tracks that process
                 (plus its children) instead of system-wide RAM.
        """
        self.interval = interval
        self.monitor_gpu = monitor_gpu
        self.pid = pid
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

        # Set up process handle for process-specific RAM tracking
        proc = None
        if self.pid:
            try:
                proc = psutil.Process(self.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                proc = None

        while not self._stop_event.is_set():
            ram_bytes = 0
            if proc:
                try:
                    ram_bytes = proc.memory_info().rss
                    for child in proc.children(recursive=True):
                        try:
                            ram_bytes += child.memory_info().rss
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    ram_bytes = 0
            else:
                ram_bytes = psutil.virtual_memory().used

            sample = ResourceSample(
                timestamp=round(time.time() - self._start_time, 1),
                ram_gb=round(ram_bytes / (1024**3), 2),
                vram_gb=self._get_gpu_vram_gb() if self.monitor_gpu else None,
            )
            self.samples.append(sample)
            self._stop_event.wait(self.interval)

    def _get_gpu_vram_gb(self) -> float | None:
        """Get GPU VRAM usage in GB via nvidia-smi (Linux/Windows only)."""
        if platform.system() == "Darwin":
            return None

        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                mib = float(result.stdout.strip().split("\n")[0])
                return round(mib / 1024, 2)
        except Exception:
            pass

        return None

    def get_summary(self) -> dict:
        """Return summary stats and timeline."""
        import psutil

        if not self.samples:
            return {}

        ram_vals = [s.ram_gb for s in self.samples]
        vram_vals = [s.vram_gb for s in self.samples if s.vram_gb is not None]

        total_ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)

        summary = {
            "ram": {"peak": max(ram_vals), "avg": round(sum(ram_vals) / len(ram_vals), 2)},
            "total_ram_gb": total_ram_gb,
            "samples": len(self.samples),
        }
        if vram_vals:
            summary["vram"] = {"peak": max(vram_vals), "avg": round(sum(vram_vals) / len(vram_vals), 2)}

        timeline = [
            {"t": s.timestamp, "ram": s.ram_gb, "vram": s.vram_gb}
            for s in self.samples
        ]
        summary["timeline"] = timeline

        return summary
