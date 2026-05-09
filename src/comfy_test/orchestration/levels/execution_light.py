"""EXECUTION_LIGHT level - Run workflows server-side, take ONE screenshot per workflow.

Same workflow execution as EXECUTION, but skips the per-frame video capture
(`capture_execution_frames`). The capture-loop in EXECUTION pegs the
browser process at 100% CPU on weak runners (macos-cpu / 7 GB) and the
Playwright IPC pipe eventually dies. Here the browser sits idle while we
run the workflow via Python-side WebSocket polling, then we take exactly
one screenshot at the end.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ...common.errors import (
    TestError,
    WorkflowError,
    WorkflowExecutionError,
    TestTimeoutError,
)
from ...common.resource_monitor import ResourceMonitor
from ...comfyui.workflow import WorkflowRunner
from ..context import LevelContext
from ..results import get_hardware_info, get_workflow_timeout


def run(ctx: LevelContext) -> LevelContext:
    """Run EXECUTION_LIGHT level.

    For each workflow: execute via API (no browser polling), take one
    static screenshot at the end, save results.

    Args:
        ctx: Level context (must have server, api set)

    Returns:
        Unchanged context

    Raises:
        WorkflowExecutionError: If any workflow fails
    """
    ctx.log(f"\n[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")

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
                novram=ctx.novram,
                vram_debug=ctx.vram_debug,
            )
        server.start()
        ctx = ctx.with_updates(server=server, api=server.get_api())

    workflows = ctx.config.workflow.workflows
    platform_config = ctx.config.get_platform_config(ctx.platform_name)

    if not workflows:
        ctx.log("No workflows configured for execution_light")
        return ctx

    if platform_config.skip_workflow:
        ctx.log("Skipped per platform config")
        return ctx

    if ctx.workflow_filter:
        workflows = [
            w for w in workflows
            if w == ctx.workflow_filter or Path(w).name == ctx.workflow_filter
        ]
        if not workflows:
            raise TestError(f"Workflow not found: {ctx.workflow_filter}")
        ctx.log(f"Workflow filter: running only {workflows[0]}")

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

    total_workflows = len(workflows)
    ctx.log(f"Running {total_workflows} workflow(s) (light mode: 1 screenshot per workflow)...")

    # Per-workflow log capture (mirrors execution.py)
    current_workflow_log: List[str] = []

    def capture_log(msg):
        current_workflow_log.append(msg)

    # WorkflowRunner does Python-side WebSocket polling -- browser stays idle.
    runner = WorkflowRunner(ctx.api, log_callback=capture_log)

    # Initialize Playwright for the single end-of-workflow screenshot.
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
    ws = WorkflowScreenshot(ctx.server.base_url, width=width, height=height, log_callback=ctx.log)
    ws.start()

    screenshots_dir = ctx.output_base / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    results = []
    logs_dir = ctx.output_base / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    hardware = get_hardware_info()

    try:
        all_errors = []

        for idx, workflow_file in enumerate(workflows, 1):
            ctx.api.free_memory(unload_models=True)
            current_workflow_log.clear()
            ctx.server.add_log_listener(capture_log)
            start_time = time.time()
            status = "pass"
            error_msg = None

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

            print(f"executing {workflow_file.name} [{idx}/{total_workflows}]")

            is_gpu_test = os.environ.get("COMFY_TEST_GPU") == "1"
            server_pid = getattr(ctx.server, 'pid', None)
            resource_monitor = ResourceMonitor(interval=1.0, monitor_gpu=is_gpu_test, pid=server_pid)
            resource_monitor.start()

            try:
                workflow_path = _resolve_workflow_path(ctx, workflow_file)
                # Run the workflow (Python-side, browser uninvolved).
                runner.run_workflow(
                    workflow_path,
                    timeout=get_workflow_timeout(ctx.config.workflow.timeout),
                )
                # Single end-of-workflow screenshot.
                final_screenshot_path = screenshots_dir / f"{workflow_file.stem}_executed.png"
                try:
                    ws.capture(workflow_path, output_path=final_screenshot_path)
                    capture_log(f"    Saved screenshot: {final_screenshot_path.name}")
                except ScreenshotError as e:
                    capture_log(f"    Screenshot failed (non-fatal): {e}")
            except (WorkflowError, TestTimeoutError) as e:
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
                mins, secs = divmod(int(duration), 60)
                metrics = []
                if peak_vram is not None:
                    metrics.append(f"Peak VRAM: {peak_vram:.2f} GB")
                if peak_ram is not None:
                    metrics.append(f"Peak RAM: {peak_ram:.2f} GB")
                metrics_str = f"  ({' | '.join(metrics)})" if metrics else ""
                print(f"[{mins:02d}:{secs:02d}] {workflow_file.name} [{idx}/{total_workflows}] - {'PASS' if status == 'pass' else 'FAIL'}{metrics_str}")
                ctx.server.remove_log_listener(capture_log)

                if resource_metrics.get("timeline"):
                    csv_path = logs_dir / f"{workflow_file.stem}_resources.csv"
                    total_ram = resource_metrics.get("total_ram_gb", 16)
                    with open(csv_path, 'w', encoding='utf-8') as f:
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

                (logs_dir / f"{workflow_file.stem}.log").write_text(
                    "\n".join(current_workflow_log), encoding="utf-8"
                )

                try:
                    ctx.api.free_memory(unload_models=True)
                except Exception:
                    pass

    finally:
        ws.stop()

    passed_count = sum(1 for r in results if r["status"] == "pass")
    failed_count = sum(1 for r in results if r["status"] == "fail")

    commit_hash = None
    if (ctx.node_dir / ".git").exists():
        try:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", str(ctx.node_dir)],
                capture_output=True, timeout=5,
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ctx.node_dir, capture_output=True, text=True, timeout=5,
            )
            if hash_result.returncode == 0:
                commit_hash = hash_result.stdout.strip()
        except Exception:
            pass

    results_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": ctx.platform_name,
        "hardware": hardware,
        "commit_hash": commit_hash,
        "success": all(r["status"] == "pass" for r in results if r["status"] != "skipped"),
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": failed_count,
        },
        "workflows": results,
    }
    results_file = ctx.output_base / "results.json"
    results_file.write_text(json.dumps(results_data, indent=2), encoding='utf-8')
    ctx.log(f"Results saved to {results_file}")

    if ctx.paths and ctx.paths.comfyui_dir:
        from ..model_tracker import build_models_report, save_models_report
        models_dir = ctx.paths.comfyui_dir / "models"
        if models_dir.exists():
            report = build_models_report(models_dir)
            if report["folders"]:
                report_path = save_models_report(report, ctx.output_base)
                ctx.log(f"Model report: {report['summary']['total_files']} files, "
                        f"{report['summary']['total_size_human']} -> {report_path}")

    from ...reporting.html_report import generate_html_report
    html_file = generate_html_report(ctx.output_base, ctx.node_dir.name)
    ctx.log(f"Saved: {html_file}")

    if all_errors:
        raise WorkflowExecutionError(
            f"Workflow execution failed ({len(all_errors)} error(s))",
            [f"{name}: {err}" for name, err in all_errors],
        )

    return ctx


def _resolve_workflow_path(ctx: LevelContext, workflow_file: Path) -> Path:
    workflow_path = Path(workflow_file)
    if not workflow_path.is_absolute():
        workflow_path = ctx.node_dir / workflow_file
    return workflow_path
