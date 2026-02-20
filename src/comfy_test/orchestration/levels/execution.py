"""EXECUTION level - Run workflows and capture results."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ...common.errors import TestError, WorkflowError, WorkflowExecutionError, TestTimeoutError
from ...common.resource_monitor import ResourceMonitor
from ..context import LevelContext
from ..results import get_hardware_info, get_workflow_timeout


class ProgressSpinner:
    """Progress indicator for workflow execution."""

    def __init__(self, workflow_name: str, current: int, total: int):
        self.workflow_name = workflow_name
        self.current = current
        self.total = total
        self.start_time = time.time()
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the spinner animation in a background thread."""
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        """Print workflow start - no animation."""
        line = f"executing {self.workflow_name} [{self.current}/{self.total}]"
        print(line)
        while not self._stop:
            time.sleep(0.1)

    def stop(self, status: str) -> None:
        """Stop and print final status."""
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.3)
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        print(f"[{mins:02d}:{secs:02d}] {self.workflow_name} [{self.current}/{self.total}] - {status}")


def run(ctx: LevelContext) -> LevelContext:
    """Run EXECUTION level.

    Executes all configured workflows, capturing video frames, logs,
    and resource metrics. Generates results.json and HTML report.

    Args:
        ctx: Level context (must have server, api set)

    Returns:
        Unchanged context

    Raises:
        WorkflowExecutionError: If any workflow fails
    """
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    # Set up server connection if not already done (e.g., when skipping registration)
    if ctx.server is None:
        from ...comfyui.server import ComfyUIServer, ExternalComfyUIServer
        if ctx.server_url:
            ctx.log(f"Connecting to existing server at {ctx.server_url}...")
            server = ExternalComfyUIServer(ctx.server_url, log_callback=ctx.log)
        else:
            ctx.log("Starting ComfyUI server...")
            server = ComfyUIServer(
                ctx.platform,
                ctx.paths,
                ctx.config,
                cuda_mock_packages=list(ctx.cuda_packages) if ctx.cuda_packages else [],
                log_callback=ctx.log,
                env_vars=ctx.env_vars if ctx.env_vars else {},
            )
        server.start()
        ctx = ctx.with_updates(server=server, api=server.get_api())

    workflows = ctx.config.workflow.workflows
    platform_config = ctx.config.get_platform_config(ctx.platform_name)

    if not workflows:
        ctx.log("No workflows configured for execution")
        return ctx

    if platform_config.skip_workflow:
        ctx.log("Skipped per platform config")
        return ctx

    # Filter workflows if requested
    if ctx.workflow_filter:
        workflows = [
            w for w in workflows
            if w == ctx.workflow_filter or Path(w).name == ctx.workflow_filter
        ]
        if not workflows:
            raise TestError(f"Workflow not found: {ctx.workflow_filter}")
        ctx.log(f"Workflow filter: running only {workflows[0]}")

    # Determine runner type and which workflows to run
    is_gpu_runner = os.environ.get("COMFY_TEST_GPU") == "1"
    cpu_workflows = set(ctx.config.workflow.cpu or [])
    gpu_workflows = set(ctx.config.workflow.gpu or [])

    if is_gpu_runner:
        allowed_workflows = gpu_workflows
        runner_type = "GPU"
    else:
        allowed_workflows = cpu_workflows
        runner_type = "CPU"

    if allowed_workflows:
        ctx.log(f"{runner_type} runner - will execute {len(allowed_workflows)} workflow(s)")
    else:
        ctx.log(f"{runner_type} runner - no workflows configured for this runner type")

    total_workflows = len(workflows)
    ctx.log(f"Running {total_workflows} workflow(s) (all with videos)...")

    # Log capture for workflow-specific logs
    current_workflow_log: List[str] = []

    def capture_log(msg):
        """Append to per-workflow log (used by server log listener)."""
        current_workflow_log.append(msg)

    def capture_and_print(msg):
        """Append to per-workflow log AND print to session log (used by WorkflowScreenshot)."""
        current_workflow_log.append(msg)
        ctx.log(msg)

    # Initialize screenshot/video capture (Playwright required)
    from ...reporting.screenshot import (
        WorkflowScreenshot,
        ScreenshotError,
        check_dependencies,
        ensure_dependencies,
    )

    python_path = ctx.paths.python if ctx.paths else None
    if not ensure_dependencies(python_path=python_path, log_callback=ctx.log):
        raise TestError("Failed to install screenshot dependencies (playwright required)")
    check_dependencies()

    ws = WorkflowScreenshot(ctx.server.base_url, log_callback=capture_and_print)
    ws.start()

    screenshots_dir = ctx.output_base / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    videos_dir = ctx.output_base / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    # Initialize results tracking
    results = []
    logs_dir = ctx.output_base / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    hardware = get_hardware_info()

    try:
        all_errors = []

        for idx, workflow_file in enumerate(workflows, 1):
            # Unload models and clear cache before each workflow
            ctx.api.free_memory(unload_models=True)

            # Reset workflow log
            current_workflow_log.clear()
            ctx.server.add_log_listener(capture_log)
            start_time = time.time()
            status = "pass"
            error_msg = None

            # Skip workflows not configured for this runner type
            if allowed_workflows and workflow_file not in allowed_workflows:
                ctx.log(f"  [{idx}/{total_workflows}] SKIPPED (not in {runner_type.lower()} list) {workflow_file.name}")
                results.append({
                    "name": workflow_file.stem,
                    "status": "skipped",
                    "duration_seconds": 0,
                    "error": f"Not configured for {runner_type} runner",
                    "hardware": None,
                })
                continue

            spinner = ProgressSpinner(workflow_file.name, idx, total_workflows)
            spinner.start()

            is_gpu_test = os.environ.get("COMFY_TEST_GPU") == "1"
            server_pid = getattr(ctx.server, 'pid', None)
            resource_monitor = ResourceMonitor(interval=1.0, monitor_gpu=is_gpu_test, pid=server_pid)
            resource_monitor.start()

            try:
                workflow_video_dir = videos_dir / workflow_file.stem
                final_screenshot_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                frames = ws.capture_execution_frames(
                    _resolve_workflow_path(ctx, workflow_file),
                    output_dir=workflow_video_dir,
                    log_lines=current_workflow_log,
                    webp_quality=60,
                    final_screenshot_path=final_screenshot_path,
                    final_screenshot_delay_ms=5000,
                    timeout=get_workflow_timeout(ctx.config.workflow.timeout),
                )
                capture_log(f"    Captured {len(frames)} video frames")
            except (WorkflowError, TestTimeoutError, ScreenshotError) as e:
                status = "fail"
                error_msg = str(e)
                capture_log("    Status: FAILED")
                capture_log(f"    Error: {e.message}")
                if hasattr(e, 'details') and e.details:
                    capture_log(f"    Details: {e.details}")
                all_errors.append((workflow_file.name, str(e)))
            except Exception as e:
                status = "fail"
                error_msg = str(e)
                capture_log("    Status: FAILED (unexpected error)")
                capture_log(f"    Error: {e}")
                all_errors.append((workflow_file.name, str(e)))
            finally:
                spinner.stop("PASS" if status == "pass" else "FAIL")
                ctx.server.remove_log_listener(capture_log)

                duration = time.time() - start_time
                resource_metrics = resource_monitor.stop()

                # Save resource timeline to CSV
                if resource_metrics.get("timeline"):
                    csv_path = logs_dir / f"{workflow_file.stem}_resources.csv"
                    total_ram = resource_metrics.get("total_ram_gb", 16)
                    with open(csv_path, 'w') as f:
                        f.write(f"# total_ram_gb={total_ram}\n")
                        f.write("t,ram_gb,vram_gb\n")
                        for sample in resource_metrics["timeline"]:
                            vram_val = sample['vram'] if sample['vram'] is not None else ''
                            f.write(f"{sample['t']},{sample['ram']},{vram_val}\n")
                    resource_metrics.pop("timeline", None)

                results.append({
                    "name": workflow_file.stem,
                    "status": status,
                    "duration_seconds": round(duration, 2),
                    "error": error_msg,
                    "hardware": hardware,
                    "resources": resource_metrics,
                })

                # Save per-workflow log (always, even on failure)
                (logs_dir / f"{workflow_file.stem}.log").write_text(
                    "\n".join(current_workflow_log), encoding="utf-8"
                )
                ws.save_console_logs(logs_dir / f"{workflow_file.stem}_console.log")
                ws.clear_console_logs()

                # Unload models after each workflow to prevent OOM on limited VRAM GPUs
                try:
                    ctx.api.free_memory(unload_models=True)
                except Exception:
                    pass

    finally:
        ws.stop()

    # Save results.json
    passed_count = sum(1 for r in results if r["status"] == "pass")
    failed_count = sum(1 for r in results if r["status"] == "fail")
    results_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": ctx.platform_name,
        "hardware": hardware,
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": failed_count
        },
        "workflows": results
    }
    results_file = ctx.output_base / "results.json"
    results_file.write_text(json.dumps(results_data, indent=2), encoding='utf-8')
    ctx.log(f"Results saved to {results_file}")

    # Log model directory state
    if ctx.paths and ctx.paths.comfyui_dir:
        from ..model_tracker import build_models_report, save_models_report
        models_dir = ctx.paths.comfyui_dir / "models"
        if models_dir.exists():
            report = build_models_report(models_dir)
            if report["folders"]:
                report_path = save_models_report(report, ctx.output_base)
                ctx.log(f"Model report: {report['summary']['total_files']} files, "
                        f"{report['summary']['total_size_human']} -> {report_path}")

    # Generate HTML report
    from ...reporting.html_report import generate_html_report
    html_file = generate_html_report(ctx.output_base, ctx.node_dir.name)
    ctx.log(f"Saved: {html_file}")

    if all_errors:
        raise WorkflowExecutionError(
            f"Workflow execution failed ({len(all_errors)} error(s))",
            [f"{name}: {err}" for name, err in all_errors]
        )

    return ctx


def _resolve_workflow_path(ctx: LevelContext, workflow_file: Path) -> Path:
    """Resolve workflow file path relative to node directory."""
    workflow_path = Path(workflow_file)
    if not workflow_path.is_absolute():
        workflow_path = ctx.node_dir / workflow_file
    return workflow_path
