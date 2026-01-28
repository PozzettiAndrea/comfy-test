"""Container pool for fast Linux test startup.

Pre-creates fresh containers so tests can start instantly without
waiting for container creation overhead (~1.5 min savings per test).
"""

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

POOL_STATE_FILE = Path.home() / ".cache" / "comfy-test" / "pool.json"
LINUX_IMAGE = "catthehacker/ubuntu:act-22.04"
DEFAULT_POOL_SIZE = 2


class ContainerPool:
    """Manages a pool of pre-created Docker containers for fast test startup."""

    def __init__(self, size: int = DEFAULT_POOL_SIZE):
        self.size = size
        self.state_file = POOL_STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _create_container(self) -> str:
        """Create a fresh container, return container ID."""
        result = subprocess.run(
            [
                "docker", "create", "-t",
                "--shm-size=8g",
                "--memory=8g",
                LINUX_IMAGE,
                "sleep", "infinity"
            ],
            capture_output=True, text=True, check=True
        )
        container_id = result.stdout.strip()
        # Start it so it's ready
        subprocess.run(
            ["docker", "start", container_id],
            check=True, capture_output=True
        )
        return container_id

    def _destroy_container(self, container_id: str):
        """Stop and remove a container."""
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

    def _load_state(self) -> List[str]:
        """Load pool state (list of container IDs)."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except (json.JSONDecodeError, IOError):
                return []
        return []

    def _save_state(self, containers: List[str]):
        """Save pool state."""
        self.state_file.write_text(json.dumps(containers))

    def _is_container_running(self, container_id: str) -> bool:
        """Check if container is still running."""
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
            capture_output=True, text=True
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def start(self, log: Callable[[str], None] = print):
        """Start the pool with N fresh containers."""
        # Clean up any existing containers first
        existing = self._load_state()
        if existing:
            log(f"Cleaning up {len(existing)} existing containers...")
            for cid in existing:
                self._destroy_container(cid)

        log(f"Starting container pool (size={self.size})...")
        containers = []
        for i in range(self.size):
            log(f"  Creating container {i+1}/{self.size}...")
            container_id = self._create_container()
            containers.append(container_id)
            log(f"  Ready: {container_id[:12]}")
        self._save_state(containers)
        log(f"Pool ready with {len(containers)} containers")

    def stop(self, log: Callable[[str], None] = print):
        """Stop and destroy all pool containers."""
        containers = self._load_state()
        if not containers:
            log("Pool is not running")
            return
        log(f"Stopping pool ({len(containers)} containers)...")
        for cid in containers:
            self._destroy_container(cid)
            log(f"  Destroyed: {cid[:12]}")
        self._save_state([])
        log("Pool stopped")

    def acquire(self) -> Optional[str]:
        """Get a fresh container from pool. Returns None if pool empty."""
        containers = self._load_state()
        # Filter out any dead containers
        containers = [c for c in containers if self._is_container_running(c)]
        if not containers:
            return None
        # Take one
        container_id = containers.pop(0)
        self._save_state(containers)
        # Replenish in background
        threading.Thread(target=self._replenish, daemon=True).start()
        return container_id

    def _replenish(self):
        """Add a new container to pool (runs in background)."""
        try:
            containers = self._load_state()
            if len(containers) < self.size:
                new_id = self._create_container()
                # Re-load in case state changed
                containers = self._load_state()
                containers.append(new_id)
                self._save_state(containers)
        except Exception:
            pass  # Background replenish failure is non-fatal

    def status(self) -> dict:
        """Get pool status."""
        containers = self._load_state()
        running = [c for c in containers if self._is_container_running(c)]
        return {
            "target_size": self.size,
            "ready": len(running),
            "containers": [c[:12] for c in running]
        }

    def is_running(self) -> bool:
        """Check if pool has any containers."""
        containers = self._load_state()
        return any(self._is_container_running(c) for c in containers)
