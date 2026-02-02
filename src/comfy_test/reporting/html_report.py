"""HTML report generator for test results.

This module generates an index.html file from test results that can be:
- Served locally via `ct show` for development preview
- Published to gh-pages for public visibility
"""

import html
import json
import os
import platform
import re
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Dict, List, Any, Optional
import subprocess


# Platform definitions for multi-platform index
PLATFORMS = [
    {'id': 'linux-cpu', 'label': 'Linux CPU'},
    {'id': 'linux-gpu', 'label': 'Linux GPU'},
    {'id': 'windows-cpu', 'label': 'Windows CPU'},
    {'id': 'windows-gpu', 'label': 'Windows GPU'},
    {'id': 'windows-portable-cpu', 'label': 'Win Portable CPU'},
    {'id': 'windows-portable-gpu', 'label': 'Win Portable GPU'},
    {'id': 'macos-cpu', 'label': 'macOS CPU'},
]


def _load_template(name: str) -> str:
    """Load a template file from report_templates directory."""
    return files("comfy_test.reporting.report_templates").joinpath(name).read_text()


def _get_system_info() -> dict:
    """Get system info (CPU, GPU, OS) for report header."""
    info = {
        'cpu': 'Unknown CPU',
        'gpu': 'None',
        'os': platform.system(),
    }

    cores = os.cpu_count() or 0

    # Get OS info
    try:
        if platform.system() == "Linux":
            if Path("/etc/os-release").exists():
                content = Path("/etc/os-release").read_text()
                match = re.search(r'PRETTY_NAME="([^"]+)"', content)
                if match:
                    info['os'] = match.group(1)
                else:
                    info['os'] = "Linux"
            else:
                info['os'] = "Linux"
        elif platform.system() == "Darwin":
            result = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                info['os'] = f"macOS {result.stdout.strip()}"
        elif platform.system() == "Windows":
            info['os'] = f"Windows {platform.release()}"
    except Exception:
        pass

    # Get CPU info
    try:
        if platform.system() == "Linux":
            cpuinfo = Path("/proc/cpuinfo").read_text()
            match = re.search(r"model name\s*:\s*(.+)", cpuinfo)
            if match:
                cpu_model = match.group(1).strip()
                cpu_model = re.sub(r"\s+", " ", cpu_model)
                cpu_model = re.sub(r"\(R\)|\(TM\)", "", cpu_model)
                info['cpu'] = cpu_model.strip()
        elif platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                info['cpu'] = result.stdout.strip()
        elif platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "name"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.split('\n') if l.strip() and l.strip() != "Name"]
                if lines:
                    info['cpu'] = lines[0]
    except Exception:
        pass

    if cores > 0:
        info['cpu'] = f"{info['cpu']} ({cores} cores)"

    # Get GPU info
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            gpus = [g.strip() for g in result.stdout.strip().split('\n') if g.strip()]
            if gpus:
                if len(gpus) > 1:
                    info['gpu'] = f"{gpus[0]} x{len(gpus)}"
                else:
                    info['gpu'] = gpus[0]
    except FileNotFoundError:
        pass
    except Exception:
        pass

    if info['gpu'] == 'None':
        try:
            if platform.system() == "Linux":
                result = subprocess.run(
                    ["lspci"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if 'VGA' in line or '3D controller' in line:
                            match = re.search(r':\s*(.+)$', line)
                            if match:
                                gpu_name = match.group(1).strip()
                                gpu_name = re.sub(r'^(NVIDIA|AMD|Intel) Corporation\s*', r'\1 ', gpu_name)
                                info['gpu'] = gpu_name
                                break
            elif platform.system() == "Darwin":
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    match = re.search(r'Chipset Model:\s*(.+)', result.stdout)
                    if match:
                        info['gpu'] = match.group(1).strip()
        except Exception:
            pass

    return info


def generate_html_report(
    output_dir: Path,
    repo_name: Optional[str] = None,
    current_platform: Optional[str] = None
) -> Path:
    """Generate index.html from results.json and screenshots.

    Args:
        output_dir: Directory containing results.json, screenshots/, logs/, videos/
        repo_name: Optional repository name for the header
        current_platform: Optional platform ID if this is a multi-platform subdir

    Returns:
        Path to the generated index.html file
    """
    results_file = output_dir / "results.json"
    screenshots_dir = output_dir / "screenshots"
    logs_dir = output_dir / "logs"
    videos_dir = output_dir / "videos"

    if not results_file.exists():
        raise FileNotFoundError(f"No results.json found in {output_dir}")

    results = json.loads(results_file.read_text(encoding='utf-8-sig'))

    # Discover available screenshots and logs
    screenshots = {f.stem.replace("_executed", ""): f.name
                   for f in screenshots_dir.glob("*.png")} if screenshots_dir.exists() else {}
    log_files = {f.stem: f.name
                 for f in logs_dir.glob("*.log")} if logs_dir.exists() else {}

    # Discover video metadata
    video_data: Dict[str, Any] = {}
    if videos_dir.exists():
        for workflow_dir in videos_dir.iterdir():
            if workflow_dir.is_dir():
                metadata_file = workflow_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        video_data[workflow_dir.name] = json.loads(metadata_file.read_text(encoding='utf-8-sig'))
                    except Exception:
                        pass

    # Read log contents
    log_contents = {}
    for name, filename in log_files.items():
        try:
            content = (logs_dir / filename).read_text(errors='replace')
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            log_contents[name] = content
        except Exception:
            log_contents[name] = "(Could not read log file)"

    # Infer repo name from directory if not provided
    if repo_name is None:
        repo_name = output_dir.parent.name
        if repo_name in (".", ".comfy-test"):
            repo_name = output_dir.parent.parent.name

    # Auto-detect platform from directory name if not provided
    if current_platform is None:
        dir_name = output_dir.name
        platform_ids = [p['id'] for p in PLATFORMS]
        if dir_name in platform_ids:
            current_platform = dir_name

    html_content = _render_report(results, screenshots, log_contents, repo_name, video_data)

    output_file = output_dir / "index.html"
    output_file.write_text(html_content, encoding="utf-8")
    return output_file


def _render_report(
    results: Dict[str, Any],
    screenshots: Dict[str, str],
    log_contents: Dict[str, str],
    repo_name: str,
    video_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the HTML report from results data."""
    video_data = video_data or {}
    summary = results.get("summary", {})
    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    workflows = results.get("workflows", [])
    timestamp = results.get("timestamp", "")
    hardware = results.get("hardware", {})

    # Parse timestamp for display
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        timestamp_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        timestamp_display = timestamp

    # Build hardware display string
    hardware_parts = []
    if hardware.get("os"):
        hardware_parts.append(hardware["os"])
    if hardware.get("cpu"):
        hardware_parts.append(hardware["cpu"])
    if hardware.get("gpu") and hardware["gpu"] != "None":
        hardware_parts.append(hardware["gpu"])
    hardware_display = " | ".join(hardware_parts) if hardware_parts else ""

    # Calculate pass rate
    pass_rate = (passed / total * 100) if total > 0 else 0

    # Render sections
    failed_workflows = [w for w in workflows if w.get("status") == "fail"]
    failed_section = _render_failed_section(failed_workflows, log_contents)
    workflow_cards, workflow_data_js = _render_workflow_cards(workflows, screenshots, log_contents)

    # Load and fill template
    template = _load_template("report.html")
    return template.format(
        repo_name=html.escape(repo_name),
        timestamp_display=timestamp_display,
        hardware_meta=f' | {hardware_display}' if hardware_display else '',
        pass_rate=pass_rate,
        pass_rate_display=f"{pass_rate:.1f}",
        progress_class=' has-failures' if failed > 0 else '',
        passed=passed,
        failed=failed,
        total=total,
        failed_badge=f'<span class="stat-badge stat-fail">{failed} FAILED</span>' if failed > 0 else '',
        failed_section=failed_section,
        workflow_cards=workflow_cards,
        workflow_data_js=workflow_data_js,
        video_data_js=json.dumps(video_data),
    )


def _render_failed_section(failed_workflows: List[Dict], log_contents: Dict[str, str]) -> str:
    """Render the failed tests section."""
    if not failed_workflows:
        return ""

    items = []
    for w in failed_workflows:
        name = w.get("name", "unknown")
        duration = w.get("duration_seconds", 0)
        error = w.get("error", "Unknown error")

        items.append(f'''
            <div class="failed-item">
                <div class="failed-header">
                    <span class="failed-name">{html.escape(name)}</span>
                    <span class="failed-duration">{duration:.2f}s</span>
                </div>
                <div class="failed-error">{html.escape(error)}</div>
            </div>
        ''')

    return f'''
        <div class="failed-section">
            <h2>Failed Tests</h2>
            {''.join(items)}
        </div>
    '''


def _render_workflow_cards(
    workflows: List[Dict],
    screenshots: Dict[str, str],
    log_contents: Dict[str, str],
) -> tuple:
    """Render workflow cards for the grid.

    Returns:
        Tuple of (cards_html, workflow_data_js)
    """
    cards = []
    workflow_data = {}

    for w in workflows:
        name = w.get("name", "unknown")
        status = w.get("status", "unknown")
        duration = w.get("duration_seconds", 0)
        hardware = w.get("hardware")
        screenshot_file = screenshots.get(name, "")
        log_content = log_contents.get(name, "")

        src = f'screenshots/{screenshot_file}' if screenshot_file else ''
        workflow_data[name] = {
            'src': src,
            'title': name,
            'status': status,
            'duration': f'{duration:.2f}',
            'log': log_content,
            'hardware': hardware,
        }

        failed_class = "failed" if status == "fail" else ""
        name_escaped = html.escape(name).replace("'", "\\'")

        if screenshot_file:
            screenshot_html = f'''
                <img class="workflow-screenshot" src="screenshots/{screenshot_file}"
                     alt="{html.escape(name)}" loading="lazy">
            '''
        else:
            screenshot_html = '<div class="workflow-screenshot placeholder">No screenshot</div>'

        onclick = f"""onclick="openLightboxByName('{name_escaped}')\""""

        cards.append(f'''
            <div class="workflow-card clickable {failed_class}" {onclick}>
                {screenshot_html}
                <div class="workflow-info">
                    <div class="workflow-header">
                        <span class="workflow-name" title="{html.escape(name)}">{html.escape(name)}</span>
                        <span class="workflow-badge {status}">{status}</span>
                    </div>
                    <div class="workflow-meta">
                        <span>{duration:.2f}s</span>
                    </div>
                </div>
            </div>
        ''')

    workflow_data_js = json.dumps(workflow_data)
    return '\n'.join(cards), workflow_data_js


def generate_root_index(output_dir: Path, repo_name: Optional[str] = None) -> Path:
    """Generate root index.html with platform tabs.

    Args:
        output_dir: Parent directory containing platform subdirectories
        repo_name: Optional repository name for the header

    Returns:
        Path to the generated index.html file
    """
    title = f"{repo_name} Test Results" if repo_name else "ComfyUI Test Results"

    template = _load_template("root_index.html")
    html_content = template.format(
        title=html.escape(title),
        platforms_json=json.dumps(PLATFORMS),
    )

    index_file = output_dir / 'index.html'
    index_file.write_text(html_content, encoding='utf-8')
    return index_file


def generate_branch_root_index(output_dir: Path, repo_name: Optional[str] = None) -> Path:
    """Generate root index.html with branch switcher tabs.

    Args:
        output_dir: Parent directory containing branch subdirectories
        repo_name: Optional repository name for the header

    Returns:
        Path to the generated index.html file
    """
    title = f"{repo_name} Test Results" if repo_name else "ComfyUI Test Results"

    # Discover available branches
    branches = []
    for subdir in sorted(output_dir.iterdir()):
        if subdir.is_dir() and (subdir / 'index.html').exists():
            if subdir.name not in [p['id'] for p in PLATFORMS]:
                branches.append(subdir.name)

    # Ensure 'main' is first if it exists
    if 'main' in branches:
        branches.remove('main')
        branches.insert(0, 'main')

    template = _load_template("branch_index.html")
    html_content = template.format(
        title=html.escape(title),
        repo_name=html.escape(repo_name or ''),
        branches_json=json.dumps(branches),
    )

    index_file = output_dir / 'index.html'
    index_file.write_text(html_content, encoding='utf-8')
    return index_file


def has_platform_subdirs(output_dir: Path) -> bool:
    """Check if output_dir has platform subdirectories with results."""
    for p in PLATFORMS:
        platform_dir = output_dir / p['id']
        if (platform_dir / 'index.html').exists():
            return True
    return False
