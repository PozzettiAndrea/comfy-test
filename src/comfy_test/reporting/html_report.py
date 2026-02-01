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
from string import Template
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

    # Load models report if available
    models_data = None
    models_file = output_dir / "models.json"
    if models_file.exists():
        try:
            models_data = json.loads(models_file.read_text(encoding='utf-8-sig'))
        except Exception:
            pass

    html_content = _render_report(results, screenshots, log_contents, repo_name, video_data, models_data)

    output_file = output_dir / "index.html"
    output_file.write_text(html_content, encoding="utf-8")
    return output_file


def _render_report(
    results: Dict[str, Any],
    screenshots: Dict[str, str],
    log_contents: Dict[str, str],
    repo_name: str,
    video_data: Optional[Dict[str, Any]] = None,
    models_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the HTML report from results data."""
    video_data = video_data or {}
    summary = results.get("summary", {})
    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    workflows = results.get("workflows", [])
    skipped = sum(1 for w in workflows if w.get("status") == "skipped")
    total = total - skipped
    timestamp = results.get("timestamp", "")
    hardware = results.get("hardware", {})

    # Parse timestamp for display
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        timestamp_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        timestamp_display = timestamp

    # Build metadata chips
    meta_chips_parts = []
    svg_clock = '<svg viewBox="0 0 16 16"><path d="M8 0a8 8 0 110 16A8 8 0 018 0zm.5 4.5a.5.5 0 00-1 0v3.793L5.854 9.854a.5.5 0 10.708.708l1.853-1.854a.5.5 0 00.085-.094V4.5z"/></svg>'
    svg_cpu = '<svg viewBox="0 0 16 16"><path d="M5 0a.5.5 0 01.5.5V2h1V.5a.5.5 0 011 0V2h1V.5a.5.5 0 011 0V2h.5A2.5 2.5 0 0112.5 4.5H14v1h-1.5v1H14v1h-1.5v1H14v1h-1.5A2.5 2.5 0 0110 12h-.5v1.5a.5.5 0 01-1 0V12h-1v1.5a.5.5 0 01-1 0V12H6v1.5a.5.5 0 01-1 0V12h-.5A2.5 2.5 0 012 9.5H.5v-1H2v-1H.5v-1H2v-1H.5v-1H2A2.5 2.5 0 014.5 2V.5A.5.5 0 015 0zm-.5 4A1.5 1.5 0 003 5.5v4A1.5 1.5 0 004.5 11h6a1.5 1.5 0 001.5-1.5v-4A1.5 1.5 0 0010.5 4h-6z"/></svg>'
    svg_gpu = '<svg viewBox="0 0 16 16"><path d="M4 8a1.5 1.5 0 113 0 1.5 1.5 0 01-3 0zm7.5-1.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM3 2a2 2 0 00-2 2v8a2 2 0 002 2h10a2 2 0 002-2V4a2 2 0 00-2-2H3z"/></svg>'
    svg_os = '<svg viewBox="0 0 16 16"><path d="M2.5 4a.5.5 0 100 1 .5.5 0 000-1zM4 4.5a.5.5 0 01.5-.5h7a.5.5 0 010 1h-7a.5.5 0 01-.5-.5zM2.5 7a.5.5 0 100 1 .5.5 0 000-1zM4 7.5a.5.5 0 01.5-.5h7a.5.5 0 010 1h-7A.5.5 0 014 7.5zM2.5 10a.5.5 0 100 1 .5.5 0 000-1zM4 10.5a.5.5 0 01.5-.5h7a.5.5 0 010 1h-7a.5.5 0 01-.5-.5z"/></svg>'
    if timestamp_display:
        meta_chips_parts.append(f'<span class="meta-chip">{svg_clock} {html.escape(timestamp_display)}</span>')
    if hardware.get("os"):
        meta_chips_parts.append(f'<span class="meta-chip">{svg_os} {html.escape(hardware["os"])}</span>')
    if hardware.get("cpu"):
        meta_chips_parts.append(f'<span class="meta-chip">{svg_cpu} {html.escape(hardware["cpu"])}</span>')
    if hardware.get("gpu") and hardware["gpu"] != "None":
        meta_chips_parts.append(f'<span class="meta-chip">{svg_gpu} {html.escape(hardware["gpu"])}</span>')
    meta_chips = ' '.join(meta_chips_parts)

    # Calculate pass rate
    pass_rate = (passed / total * 100) if total > 0 else 0

    # Render sections
    failed_workflows = [w for w in workflows if w.get("status") == "fail"]
    failed_section = _render_failed_section(failed_workflows, log_contents)
    workflow_cards, workflow_data_js = _render_workflow_cards(workflows, screenshots, log_contents)
    models_section = _render_models_section(models_data)

    # Load and fill template (using string.Template to avoid escaping CSS braces)
    template = Template(_load_template("report.html"))
    return template.safe_substitute(
        repo_name=html.escape(repo_name),
        meta_chips=meta_chips,
        pass_rate=pass_rate,
        pass_rate_display=f"{pass_rate:.1f}",
        progress_class=' has-failures' if failed > 0 else '',
        passed=passed,
        failed=failed,
        total=total,
        failed_badge=f'<span class="stat-badge stat-fail">{failed} failed</span>' if failed > 0 else '',
        failed_section=failed_section,
        workflow_cards=workflow_cards,
        workflow_data_js=workflow_data_js,
        video_data_js=json.dumps(video_data),
        models_section=models_section,
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
        if status == "skipped":
            continue
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


def _render_models_section(models_data: Optional[Dict[str, Any]]) -> str:
    """Render the downloaded models section."""
    if not models_data or not models_data.get("folders"):
        return ""

    summary = models_data.get("summary", {})
    folders = models_data.get("folders", {})

    folder_items = []
    for folder_name, folder_info in folders.items():
        files_list = folder_info.get("files", [])
        source = folder_info.get("source", {})
        folder_size = folder_info.get("total_size_human", "")

        # Source badge
        source_html = ""
        if source.get("type") == "huggingface":
            repo = html.escape(source.get("repo", ""))
            url = html.escape(source.get("url", ""))
            source_html = f'<a class="models-source-link" href="{url}" target="_blank" rel="noopener">HF: {repo}</a>'

        # File rows
        file_rows = []
        for f in files_list:
            path = html.escape(f.get("path", ""))
            size = html.escape(f.get("size_human", ""))
            file_rows.append(
                f'<div class="models-file">'
                f'<span class="models-file-path">{path}</span>'
                f'<span class="models-file-size">{size}</span>'
                f'</div>'
            )

        files_html = "\n".join(file_rows)
        folder_items.append(f'''
            <details class="models-folder" open>
                <summary class="models-folder-header">
                    <span class="models-folder-name">models/{html.escape(folder_name)}/</span>
                    <span class="models-folder-meta">
                        {source_html}
                        <span class="models-folder-size">{html.escape(folder_size)}</span>
                    </span>
                </summary>
                <div class="models-file-list">
                    {files_html}
                </div>
            </details>
        ''')

    total_size = html.escape(summary.get("total_size_human", "0 B"))
    total_files = summary.get("total_files", 0)

    return f'''
        <div class="models-section">
            <div class="section-header">
                <h2 class="section-title">Downloaded Models</h2>
                <span class="models-total">{total_files} files &middot; {total_size}</span>
            </div>
            {''.join(folder_items)}
        </div>
    '''


def generate_root_index(output_dir: Path, repo_name: Optional[str] = None) -> Path:
    """Generate root index.html with platform tabs.

    Args:
        output_dir: Parent directory containing platform subdirectories
        repo_name: Optional repository name for the header

    Returns:
        Path to the generated index.html file
    """
    title = f"{repo_name} Test Results" if repo_name else "ComfyUI Test Results"

    template = Template(_load_template("root_index.html"))
    html_content = template.safe_substitute(
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

    template = Template(_load_template("branch_index.html"))
    html_content = template.safe_substitute(
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
