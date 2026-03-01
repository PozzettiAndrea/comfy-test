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
        self._pids: set[int] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_time: float = 0

    def start(self):
        """Start monitoring in background thread."""
        self._stop_event.clear()
        self.samples = []
        self._pids: set[int] = set()
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
                    children = proc.children(recursive=True)
                    self._pids = {proc.pid} | {c.pid for c in children}
                    ram_bytes = proc.memory_info().rss
                    for child in children:
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
        """Get GPU VRAM usage in GB via nvidia-smi.

        When a PID is set, queries per-process GPU memory for that process
        and its children. Falls back to system-wide GPU memory if per-process
        query fails or no PID is set.
        """
        if platform.system() == "Darwin":
            return None

        if self.pid and self._pids:
            vram = self._get_per_process_vram_gb()
            if vram is not None:
                return vram

        # Fallback: system-wide GPU memory
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

    def _get_per_process_vram_gb(self) -> float | None:
        """Get GPU VRAM for tracked PIDs via nvidia-smi per-process query.

        Returns 0.0 when the query succeeds but no tracked PIDs have GPU
        allocations (valid: process isn't using VRAM right now).
        Returns None only on query failure (caller should fall back).
        """
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return None

            total_mib = 0.0
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) >= 2:
                    pid = int(parts[0].strip())
                    mib = float(parts[1].strip())
                    if pid in self._pids:
                        total_mib += mib

            return round(total_mib / 1024, 2)
        except Exception:
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
