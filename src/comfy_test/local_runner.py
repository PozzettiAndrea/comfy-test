"""Local test execution via act (GitHub Actions locally)."""

import subprocess
import shutil
import tempfile
import time
import re
import sys
from pathlib import Path
from typing import Callable, Optional, List, Tuple

ACT_IMAGE = "catthehacker/ubuntu:act-22.04"


def ensure_gitignore(node_dir: Path, pattern: str = ".comfy-test-logs/"):
    """Add pattern to .gitignore if not already present."""
    gitignore = node_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if pattern.rstrip('/') not in content:
            with open(gitignore, "a") as f:
                f.write(f"\n# comfy-test output\n{pattern}\n")
    else:
        gitignore.write_text(f"# comfy-test output\n{pattern}\n")


# Patterns to detect step transitions in act output
STEP_START = re.compile(r'^Run (?:Main |Post )?(.+)$')
STEP_SUCCESS = re.compile(r'^Success - (?:Main |Post )?(.+?) \[')
STEP_FAILURE = re.compile(r'^Failure - (?:Main |Post )?(.+?) \[')


def _gitignore_filter(base_dir: Path):
    """Create a shutil.copytree ignore function based on .gitignore patterns."""
    import fnmatch

    # Always ignore these (essential for clean copy)
    # Note: .git is NOT ignored - workflow needs it for checkout step
    always_ignore = {'__pycache__', '.comfy-test', '.comfy-test-env', '.comfy-test-logs'}

    # Parse .gitignore if it exists
    gitignore_patterns = []
    gitignore_file = base_dir / ".gitignore"
    if gitignore_file.exists():
        for line in gitignore_file.read_text().splitlines():
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Remove trailing slashes (we match both files and dirs)
            pattern = line.rstrip('/')
            gitignore_patterns.append(pattern)

    def ignore_func(directory: str, names: List[str]) -> List[str]:
        ignored = []
        rel_dir = Path(directory).relative_to(base_dir) if directory != str(base_dir) else Path('.')

        for name in names:
            # Always ignore these
            if name in always_ignore:
                ignored.append(name)
                continue

            # Check gitignore patterns
            rel_path = rel_dir / name
            for pattern in gitignore_patterns:
                # Match against filename and relative path
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel_path), pattern):
                    ignored.append(name)
                    break
                # Handle patterns like "dir/" matching directories
                if pattern.endswith('/') and fnmatch.fnmatch(name, pattern[:-1]):
                    ignored.append(name)
                    break

        return ignored

    return ignore_func


def split_log_by_workflow(log_file: Path, logs_dir: Path) -> int:
    """Extract per-workflow sections from main log file."""
    if not log_file.exists():
        return 0

    content = log_file.read_text()
    lines = content.splitlines()

    # Match: "executing mesh_info.json [1/23]"
    workflow_start = re.compile(r'executing (\S+)\.json\s+\[\d+/\d+\]')
    # Match: "mesh_info.json [1/23] - PASS" or "- FAIL"
    workflow_end = re.compile(r'\.json\s+\[\d+/\d+\]\s+-\s+(PASS|FAIL)')

    logs_dir.mkdir(parents=True, exist_ok=True)

    current_workflow = None
    current_lines = []
    count = 0

    for line in lines:
        match = workflow_start.search(line)
        if match:
            if current_workflow and current_lines:
                (logs_dir / f"{current_workflow}.log").write_text("\n".join(current_lines))
                count += 1
            current_workflow = match.group(1)
            current_lines = [line]
        elif current_workflow:
            current_lines.append(line)
            if workflow_end.search(line):
                (logs_dir / f"{current_workflow}.log").write_text("\n".join(current_lines))
                count += 1
                current_workflow = None
                current_lines = []

    return count


def run_local(
    node_dir: Path,
    output_dir: Path,
    config_file: str = "comfy-test.toml",
    gpu: bool = False,
    verbose: bool = False,
    log_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """Run tests locally via act (GitHub Actions in Docker).

    Args:
        node_dir: Path to the custom node directory
        output_dir: Where to save screenshots/logs/results.json
        config_file: Config file name
        gpu: Enable GPU passthrough
        verbose: Show all output (streaming mode)
        log_callback: Function to call with log lines

    Returns:
        Exit code (0 = success)
    """
    log = log_callback or print

    # Auto-add .comfy-test-logs to .gitignore
    ensure_gitignore(node_dir, ".comfy-test-logs/")

    # Verify act is installed
    if not shutil.which("act"):
        log("Error: act is not installed. Install from https://github.com/nektos/act")
        return 1

    # Verify node directory has config
    if not (node_dir / config_file).exists():
        log(f"Error: {config_file} not found in {node_dir}")
        return 1

    # Verify workflow file exists
    workflow_file = node_dir / ".github" / "workflows" / "run-tests.yml"
    if not workflow_file.exists():
        log(f"Error: {workflow_file} not found")
        return 1

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create main log file inside output_dir
    log_file = output_dir / f"{output_dir.name}.log"

    # Local paths
    local_comfy_test = Path.home() / "utils" / "comfy-test"
    local_comfy_env = Path.home() / "utils" / "comfy-env"
    local_workflow = local_comfy_test / ".github" / "workflows" / "test-matrix-local.yml"

    # Create isolated temp directory for full isolation
    temp_dir = tempfile.mkdtemp(prefix="comfy-test-")
    work_dir = Path(temp_dir) / node_dir.name

    try:
        # Copy node to temp dir, respecting .gitignore
        log(f"Copying node to isolated environment...")
        shutil.copytree(
            node_dir, work_dir,
            ignore=_gitignore_filter(node_dir)
        )

        # Set up local workflow
        if local_workflow.exists():
            work_workflow_dir = work_dir / ".github" / "workflows"
            work_workflow_dir.mkdir(parents=True, exist_ok=True)
            target = work_workflow_dir / "test-matrix.yml"

            workflow_content = local_workflow.read_text()
            repo_suffix = node_dir.name.replace("ComfyUI-", "").lower()
            workflow_content = workflow_content.replace("parse-config:", f"parse-config-{repo_suffix}:")
            workflow_content = workflow_content.replace("needs: parse-config", f"needs: parse-config-{repo_suffix}")
            workflow_content = workflow_content.replace("test-linux:", f"test-linux-{repo_suffix}:")
            workflow_content = workflow_content.replace("test-windows:", f"test-windows-{repo_suffix}:")
            workflow_content = workflow_content.replace("test-windows-portable:", f"test-windows-portable-{repo_suffix}:")

            target.write_text(workflow_content)

            run_tests_yml = work_workflow_dir / "run-tests.yml"
            if run_tests_yml.exists():
                content = run_tests_yml.read_text()
                patched = re.sub(
                    r'uses:\s*PozzettiAndrea/comfy-test/\.github/workflows/test-matrix\.yml@\w+',
                    'uses: ./.github/workflows/test-matrix.yml',
                    content
                )
                if patched != content:
                    run_tests_yml.write_text(patched)

        # Pre-build wheels on host (avoids hatchling issues in Docker)
        wheel_dir = work_dir / ".wheels"
        wheel_dir.mkdir(exist_ok=True)

        if local_comfy_test.exists():
            log(f"Building comfy-test wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_test) + "[screenshot]", "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        if local_comfy_env.exists():
            log(f"Building comfy-env wheel...")
            subprocess.run(
                ["pip", "wheel", str(local_comfy_env), "--no-deps", "--no-cache-dir", "-w", str(wheel_dir)],
                capture_output=True, check=True
            )

        # Build container options - mount output dir
        container_opts = [
            "-t",  # Allocate pseudo-TTY to force line-buffered output
            f"-v {output_dir}:{work_dir}/.comfy-test",
            "--shm-size=8g",  # Default 64MB is too small for ML tensor transfer
        ]
        if gpu:
            container_opts.append("--gpus all")

        # Build command (use temp dir for action cache to avoid stale state)
        action_cache_dir = Path(temp_dir) / ".act-cache"
        # Use unique toolcache path to isolate concurrent runs
        toolcache_path = f"/tmp/toolcache-{Path(temp_dir).name}"
        cmd = [
            "stdbuf", "-oL",
            "act",
            "-P", f"ubuntu-latest={ACT_IMAGE}",
            "--pull=false",
            "--rm",
            "-j", "test",
            "--network", "bridge",
            "--action-cache-path", str(action_cache_dir),
            "--container-options", " ".join(container_opts),
            "--env", "PYTHONUNBUFFERED=1",
            "--env", f"RUNNER_TOOL_CACHE={toolcache_path}",
        ]
        if gpu:
            cmd.extend(["--env", "COMFY_TEST_GPU=1"])

        # Patterns to strip from output
        emoji_pattern = re.compile(r'[⭐🚀🐳✅❌🏁⬇️📜✏️❓🧪🔧💬⚙️🚧☁️]')
        job_prefix_pattern = re.compile(r'\[test/[^\]]+\]\s*')
        # Detect workflow execution lines: "executing mesh_info.json [1/23]"
        workflow_pattern = re.compile(r'executing (\S+)\s+\[(\d+)/(\d+)\]')

        start_time = time.time()

        # Run with unbuffered output from isolated work_dir
        process = subprocess.Popen(
            cmd,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )

        # Track steps for summary mode
        current_step = None
        current_step_output: List[str] = []
        completed_steps: List[Tuple[str, bool, List[str]]] = []
        return_code = 0

        try:
            with open(log_file, "w") as f:
                while True:
                    if process.stdout:
                        line = process.stdout.readline()
                        if line:
                            # Strip noise: emojis and job prefix
                            clean_line = emoji_pattern.sub('', line.rstrip())
                            clean_line = job_prefix_pattern.sub('', clean_line)
                            elapsed = int(time.time() - start_time)
                            mins, secs = divmod(elapsed, 60)
                            timer = f"[{mins:02d}:{secs:02d}]"
                            formatted = f"{timer} {clean_line}"

                            # Write to log file
                            f.write(formatted + "\n")
                            f.flush()

                            if verbose:
                                # Verbose mode: stream everything
                                log(formatted)
                            else:
                                # Summary mode: track steps
                                if workflow_match := workflow_pattern.search(clean_line):
                                    # Show workflow progress
                                    name, current, total = workflow_match.groups()
                                    print(f"    {timer} Running {name} [{current}/{total}]")
                                elif match := STEP_START.search(clean_line):
                                    current_step = match.group(1)
                                    current_step_output = []
                                    print(f"  {timer} {current_step}...")
                                elif match := STEP_SUCCESS.search(clean_line):
                                    step_name = match.group(1)
                                    print(f"  {timer} {step_name}... [OK]")
                                    completed_steps.append((step_name, True, []))
                                    current_step = None
                                elif match := STEP_FAILURE.search(clean_line):
                                    step_name = match.group(1)
                                    print(f"  {timer} {step_name}... [ERROR]")
                                    completed_steps.append((step_name, False, current_step_output.copy()))
                                    current_step = None

                                # Capture output for error context
                                if current_step and clean_line.strip():
                                    current_step_output.append(clean_line)
                                    if len(current_step_output) > 20:
                                        current_step_output.pop(0)
                        elif process.poll() is not None:
                            break
                    else:
                        break
        except KeyboardInterrupt:
            process.kill()
            process.wait()
            subprocess.run(
                f"docker kill $(docker ps -q --filter ancestor={ACT_IMAGE}) 2>/dev/null",
                shell=True,
                capture_output=True,
            )
            log("\nTest cancelled")
            return_code = 130

        # Show error context for failed steps
        if not verbose and return_code != 130:
            for step_name, success, output in completed_steps:
                if not success and output:
                    log(f"\n  Error in {step_name}:")
                    for line in output[-5:]:
                        log(f"    {line}")

        # Split main log into per-workflow logs
        logs_dir = output_dir / "logs"
        if logs_dir.exists():
            subprocess.run(["sudo", "rm", "-rf", str(logs_dir)], capture_output=True)
        workflow_logs = split_log_by_workflow(log_file, logs_dir)

        # Report output
        screenshots_dir = output_dir / "screenshots"
        screenshot_files = list(screenshots_dir.glob("*.png")) if screenshots_dir.exists() else []
        results_file = output_dir / "results.json"

        if screenshot_files or results_file.exists() or log_file.exists():
            log(f"\nLog: {log_file}")
            if workflow_logs:
                log(f"Workflow logs: {workflow_logs}")
            if screenshot_files:
                log(f"Screenshots: {len(screenshot_files)}")

        if return_code != 0:
            return return_code
        return process.returncode or 0

    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)
