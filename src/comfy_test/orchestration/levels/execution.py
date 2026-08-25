"""EXECUTION level - Run workflows and capture results."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ...common.errors import TestError, WorkflowError, WorkflowExecutionError, TestTimeoutError
from ...common.resource_monitor import ResourceMonitor
from ..context import LevelContext, resolve_lane_id
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

    def stop(self, status: str, peak_vram_gb: float | None = None, peak_ram_gb: float | None = None) -> None:
        """Stop and print final status."""
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.3)
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        metrics = []
        if peak_vram_gb is not None:
            metrics.append(f"Peak VRAM: {peak_vram_gb:.2f} GB")
        if peak_ram_gb is not None:
            metrics.append(f"Peak RAM: {peak_ram_gb:.2f} GB")
        metrics_str = f"  ({' | '.join(metrics)})" if metrics else ""
        print(f"[{mins:02d}:{secs:02d}] {self.workflow_name} [{self.current}/{self.total}] - {status}{metrics_str}")


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
    ctx.log(f"\n[DEBUG] server={ctx.server}, api={ctx.api}")
    # Set up server connection if not already done (e.g., when skipping registration)
    if ctx.server is None:
        from ...comfyui.server import ComfyUIServer
        ctx.log("Starting ComfyUI server...")
        server = ComfyUIServer(
            ctx.platform,
            ctx.paths,
            ctx.config,
            log_callback=ctx.log,
            env_vars=ctx.env_vars if ctx.env_vars else {},
            novram=ctx.novram,
            vram_debug=ctx.vram_debug,
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
    is_cuda_runner = os.environ.get("COMFY_TEST_CUDA") == "1"
    cpu_workflows = set(ctx.config.workflow.cpu or [])
    cuda_workflows = set(ctx.config.workflow.cuda or [])

    if is_cuda_runner:
        allowed_workflows = cuda_workflows
        other_list = cpu_workflows
        runner_type = "CUDA"
    else:
        allowed_workflows = cpu_workflows
        other_list = cuda_workflows
        runner_type = "CPU"

    # Empty-list semantics. An empty allowed list disables the skip filter,
    # which is right for nodes that never configured routing at all -- but when
    # the OTHER accelerator's list IS configured, the node has clearly
    # expressed routing intent and an empty list here means "nothing runs on
    # this runner" (as the WorkflowConfig docstring has always said: "If
    # empty, skip CUDA jobs"). Previously this case fell through to
    # run-everything, which is how a typo'd key ('gpu' instead of 'cuda')
    # executed all 59 workflows on a runner configured to run 3, presented as
    # a plausible 48/59 result.
    if not allowed_workflows and other_list:
        ctx.log(
            f"{runner_type} runner - the {runner_type.lower()} list is empty "
            f"while the other accelerator list is configured; running nothing. "
            f"Populate [test.workflows] {runner_type.lower()} = [...] to run "
            f"workflows on this runner.")
        return ctx

    total_workflows = len(workflows)
    if allowed_workflows:
        _names = sorted(w.stem for w in workflows if w in allowed_workflows)
        _n_skipped = total_workflows - len(_names)
        ctx.log(f"{runner_type} runner - will execute {len(_names)} workflow(s): "
                f"[{', '.join(_names)}]")
        if _n_skipped:
            ctx.log(f"  ({_n_skipped} workflow(s) not in the "
                    f"{runner_type.lower()} list; recorded as skipped)")
    else:
        ctx.log(f"{runner_type} runner - no workflows configured for this runner type")
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

    height = ctx.config.res
    width = int(height * 16 / 9)
    ws = WorkflowScreenshot(ctx.server.base_url, width=width, height=height, log_callback=capture_and_print)
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
            # Skip workflows not configured for this runner type. Checked FIRST,
            # before free_memory and before registering the log listener:
            # previously every skipped iteration hit the server's /free endpoint
            # (56 pointless calls per cuda run, each echoing "Using RAM pressure
            # cache" into the log between SKIPPED lines) and leaked one log
            # listener per skip. Silent on purpose -- the executed set is
            # announced up front; skips only appear in results.json.
            if allowed_workflows and workflow_file not in allowed_workflows:
                results.append({
                    "name": workflow_file.stem,
                    "status": "skipped",
                    "duration_seconds": 0,
                    "error": f"Not configured for {runner_type} runner",
                    "hardware": None,
                })
                continue

            ctx.api.free_memory(unload_models=True)

            # Reset workflow log
            current_workflow_log.clear()
            ctx.server.add_log_listener(capture_log)
            start_time = time.time()
            status = "pass"
            error_msg = None

            spinner = ProgressSpinner(workflow_file.name, idx, total_workflows)
            spinner.start()

            is_cuda_test = os.environ.get("COMFY_TEST_CUDA") == "1"
            server_pid = getattr(ctx.server, 'pid', None)
            resource_monitor = ResourceMonitor(interval=1.0, monitor_cuda=is_cuda_test, pid=server_pid)
            resource_monitor.start()

            try:
                workflow_video_dir = videos_dir / workflow_file.stem
                final_screenshot_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                ws._last_capture_start = None  # reset; set inside on success
                ws._last_media_span = None     # video/scrubber duration, set inside
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
                duration = time.time() - start_time
                resource_metrics = resource_monitor.stop()
                peak_vram = resource_metrics.get("vram", {}).get("peak")
                peak_ram = resource_metrics.get("ram", {}).get("peak")
                spinner.stop("PASS" if status == "pass" else "FAIL", peak_vram_gb=peak_vram, peak_ram_gb=peak_ram)
                ctx.server.remove_log_listener(capture_log)

                # Save resource timeline to CSV
                if resource_metrics.get("timeline"):
                    csv_path = logs_dir / f"{workflow_file.stem}_resources.csv"
                    total_ram = resource_metrics.get("total_ram_gb", 16)
                    # Align the timeline to the video's t=0. The monitor starts
                    # before the browser navigates to ComfyUI, so its clock leads
                    # the video by the navigation time; shift by that offset and
                    # drop the pre-navigation samples so the graph and the video
                    # share one clock.
                    cap_start = getattr(ws, "_last_capture_start", None)
                    media_span = getattr(ws, "_last_media_span", None)
                    offset = (cap_start - resource_monitor._start_time) if cap_start else 0.0
                    with open(csv_path, 'w', encoding='utf-8') as f:
                        f.write(f"# total_ram_gb={total_ram}\n")
                        f.write("t,ram_gb,vram_gb\n")
                        for sample in resource_metrics["timeline"]:
                            t = round(sample['t'] - offset, 1)
                            if t < 0:
                                continue  # sampled before the video's first frame
                            if media_span is not None and t > media_span + 0.5:
                                continue  # sampled after the video ended
                            vram_val = sample['vram'] if sample['vram'] is not None else ''
                            f.write(f"{t},{sample['ram']},{vram_val}\n")
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

    # Resolve commit hash of the node being tested.
    # Only read if .git exists directly in node_dir -- don't let git walk
    # up to a parent repo (e.g., ComfyUI) and return the wrong hash.
    commit_hash = None
    _git_dir_exists = (ctx.node_dir / ".git").exists()
    print(f"[commit_hash debug] platform={ctx.platform_name} "
          f"node_dir={ctx.node_dir} .git exists={_git_dir_exists}",
          flush=True)
    if _git_dir_exists:
        try:
            # Mark dir as safe (Docker bind mounts have different ownership)
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", str(ctx.node_dir)],
                capture_output=True, timeout=5,
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ctx.node_dir, capture_output=True, text=True, timeout=5,
            )
            print(f"[commit_hash debug] git rev-parse rc={hash_result.returncode} "
                  f"stdout={hash_result.stdout.strip()!r} "
                  f"stderr={hash_result.stderr.strip()!r}",
                  flush=True)
            if hash_result.returncode == 0:
                commit_hash = hash_result.stdout.strip()
        except Exception as e:
            print(f"[commit_hash debug] exception: {e!r}", flush=True)

    from ...common.config import build_provenance
    provenance = build_provenance(
        ctx.config, install_mode="attach" if ctx.server_url else "fresh")

    results_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lane": resolve_lane_id(ctx),
        "hardware": hardware,
        "comfyui_version": ctx.comfyui_version,
        # SHA of the PACK under test (kept under the legacy name the dashboard
        # reads); ComfyUI's own SHA is provenance.comfyui_commit.
        "commit_hash": commit_hash,
        "comfyui_commit": ctx.comfyui_commit,
        "provenance": provenance,
        # GHA run that produced this result. Dashboard's Goto-mode reads it
        # to deep-link cells back to the run. Set by dispatch-test.yml's
        # job-level env (github.* expansion).
        "run_url": os.environ.get("COMFY_TEST_RUN_URL") or None,
        "success": all(r["status"] == "pass" for r in results if r["status"] != "skipped"),
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
