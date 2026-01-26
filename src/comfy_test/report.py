"""HTML report generator for test results.

This module generates an index.html file from test results that can be:
- Served locally via `ct show` for development preview
- Published to gh-pages for public visibility

The same HTML is used in both contexts, ensuring parity between local and deployed views.
"""

import html
import json
import os
import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


def _get_system_info() -> dict:
    """Get system info (CPU, GPU, OS) for report header.

    Returns:
        Dict with 'cpu', 'gpu', 'os' keys
    """
    import subprocess

    info = {
        'cpu': 'Unknown CPU',
        'gpu': 'None',
        'os': platform.system(),
    }

    cores = os.cpu_count() or 0

    # Get OS info
    try:
        if platform.system() == "Linux":
            # Try to get distro info
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
        # Try nvidia-smi first (NVIDIA GPUs)
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            gpus = [g.strip() for g in result.stdout.strip().split('\n') if g.strip()]
            if gpus:
                # If multiple GPUs, show count
                if len(gpus) > 1:
                    info['gpu'] = f"{gpus[0]} x{len(gpus)}"
                else:
                    info['gpu'] = gpus[0]
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # If no NVIDIA GPU, try other methods
    if info['gpu'] == 'None':
        try:
            if platform.system() == "Linux":
                # Try lspci for any GPU
                result = subprocess.run(
                    ["lspci"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if 'VGA' in line or '3D controller' in line:
                            # Extract GPU name after the colon
                            match = re.search(r':\s*(.+)$', line)
                            if match:
                                gpu_name = match.group(1).strip()
                                # Simplify common prefixes
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


def generate_html_report(output_dir: Path, repo_name: Optional[str] = None) -> Path:
    """Generate index.html from results.json and screenshots.

    This is the single source of truth - used for both:
    - Local preview via `ct show`
    - gh-pages publishing in CI

    Args:
        output_dir: Directory containing results.json, screenshots/, logs/, videos/
        repo_name: Optional repository name for the header

    Returns:
        Path to the generated index.html file
    """
    results_file = output_dir / "results.json"
    screenshots_dir = output_dir / "screenshots"
    logs_dir = output_dir / "logs"
    videos_dir = output_dir / "videos"

    if not results_file.exists():
        raise FileNotFoundError(f"No results.json found in {output_dir}")

    results = json.loads(results_file.read_text())

    # Discover available screenshots and logs
    screenshots = {f.stem.replace("_executed", ""): f.name
                   for f in screenshots_dir.glob("*.png")} if screenshots_dir.exists() else {}
    log_files = {f.stem: f.name
                 for f in logs_dir.glob("*.log")} if logs_dir.exists() else {}

    # Discover video metadata (from videos/{workflow_name}/metadata.json)
    video_data: Dict[str, Any] = {}
    if videos_dir.exists():
        for workflow_dir in videos_dir.iterdir():
            if workflow_dir.is_dir():
                metadata_file = workflow_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        video_data[workflow_dir.name] = json.loads(metadata_file.read_text())
                    except Exception:
                        pass

    # Read log contents
    log_contents = {}
    for name, filename in log_files.items():
        try:
            content = (logs_dir / filename).read_text(errors='replace')
            # Limit log size to prevent huge HTML files
            if len(content) > 50000:
                content = content[:50000] + "\n... (truncated)"
            log_contents[name] = content
        except Exception:
            log_contents[name] = "(Could not read log file)"

    # Infer repo name from directory if not provided
    if repo_name is None:
        # Try to get from parent directory name (e.g., ComfyUI-GeometryPack)
        repo_name = output_dir.parent.name
        if repo_name in (".", ".comfy-test"):
            repo_name = output_dir.parent.parent.name

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
    """Render the HTML report from results data.

    Args:
        results: Parsed results.json data
        screenshots: Dict mapping workflow name to screenshot filename
        log_contents: Dict mapping workflow name to log content
        repo_name: Repository name for the header
        video_data: Dict mapping workflow name to metadata (frames with timestamps/logs)

    Returns:
        Complete HTML document as string
    """
    video_data = video_data or {}
    summary = results.get("summary", {})
    total = summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    workflows = results.get("workflows", [])
    timestamp = results.get("timestamp", "")
    platform = results.get("platform", "unknown")

    # Parse timestamp for display
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        timestamp_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        timestamp_display = timestamp

    # Calculate pass rate
    pass_rate = (passed / total * 100) if total > 0 else 0

    # Separate failed and passed workflows
    failed_workflows = [w for w in workflows if w.get("status") == "fail"]
    all_workflows = workflows

    # Build workflow cards HTML
    failed_section = _render_failed_section(failed_workflows, log_contents)
    workflow_cards, workflow_data_js = _render_workflow_cards(all_workflows, screenshots, log_contents)

    # Build video frames JSON for JavaScript
    video_data_js = json.dumps(video_data)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{repo_name} - Test Results</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
            line-height: 1.5;
        }}

        header {{
            background: #16213e;
            padding: 1.5rem 2rem;
            border-bottom: 1px solid #0f3460;
        }}

        h1 {{
            font-size: 1.5rem;
            margin-bottom: 0.25rem;
        }}

        h1 a {{
            color: #fff;
            text-decoration: none;
        }}

        h1 a:hover {{ color: #4da6ff; }}

        .meta {{
            color: #888;
            font-size: 0.9rem;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 1.5rem;
        }}

        /* Summary Section */
        .summary {{
            background: #16213e;
            border-radius: 8px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }}

        .progress-bar {{
            background: #0f3460;
            border-radius: 4px;
            height: 24px;
            overflow: hidden;
            margin-bottom: 1rem;
        }}

        .progress-fill {{
            background: linear-gradient(90deg, #00c853, #69f0ae);
            height: 100%;
            transition: width 0.3s ease;
        }}

        .progress-fill.has-failures {{
            background: linear-gradient(90deg, #00c853 0%, #00c853 {pass_rate}%, #ff5252 {pass_rate}%, #ff5252 100%);
            width: 100% !important;
        }}

        .stats {{
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            align-items: center;
        }}

        .stat-badge {{
            padding: 0.5rem 1rem;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.9rem;
        }}

        .stat-pass {{
            background: rgba(0, 200, 83, 0.2);
            color: #69f0ae;
        }}

        .stat-fail {{
            background: rgba(255, 82, 82, 0.2);
            color: #ff8a80;
        }}

        .stat-total {{
            color: #888;
            font-size: 1rem;
        }}

        /* Failed Section */
        .failed-section {{
            background: rgba(255, 82, 82, 0.1);
            border: 1px solid rgba(255, 82, 82, 0.3);
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 1.5rem;
        }}

        .failed-section h2 {{
            color: #ff8a80;
            font-size: 1rem;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .failed-item {{
            background: #16213e;
            border-radius: 6px;
            padding: 1rem;
            margin-bottom: 0.5rem;
        }}

        .failed-item:last-child {{
            margin-bottom: 0;
        }}

        .failed-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
        }}

        .failed-name {{
            font-weight: 600;
            color: #ff8a80;
        }}

        .failed-duration {{
            color: #888;
            font-size: 0.85rem;
        }}

        .failed-error {{
            background: #0f3460;
            padding: 0.75rem;
            border-radius: 4px;
            font-family: monospace;
            font-size: 0.85rem;
            color: #ffa;
            margin-bottom: 0.5rem;
        }}

        .log-link {{
            color: #4da6ff;
            text-decoration: none;
            font-size: 0.85rem;
        }}

        .log-link:hover {{
            text-decoration: underline;
        }}

        /* Workflow Grid */
        .section-title {{
            font-size: 1rem;
            color: #888;
            margin-bottom: 1rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .workflow-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 1rem;
        }}

        .workflow-card {{
            background: #16213e;
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s;
        }}

        .workflow-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}

        .workflow-card.clickable {{
            cursor: pointer;
        }}

        .workflow-card.failed {{
            border: 2px solid #ff5252;
            box-shadow: 0 0 8px rgba(255, 82, 82, 0.3);
        }}

        .workflow-screenshot {{
            width: 100%;
            aspect-ratio: 16/10;
            object-fit: cover;
            background: #0f3460;
            display: block;
        }}

        .workflow-screenshot.placeholder {{
            display: flex;
            align-items: center;
            justify-content: center;
            color: #444;
            font-size: 0.85rem;
        }}

        .workflow-info {{
            padding: 0.75rem 1rem;
            border-top: 1px solid #0f3460;
        }}

        .workflow-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.25rem;
        }}

        .workflow-name {{
            font-weight: 500;
            font-size: 0.9rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 60%;
        }}

        .workflow-badge {{
            padding: 0.2rem 0.5rem;
            border-radius: 3px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }}

        .workflow-badge.pass {{
            background: rgba(0, 200, 83, 0.2);
            color: #69f0ae;
        }}

        .workflow-badge.fail {{
            background: rgba(255, 82, 82, 0.2);
            color: #ff8a80;
        }}

        .workflow-meta {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.8rem;
            color: #666;
        }}

        /* Lightbox */
        .lightbox {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.95);
            z-index: 1000;
            justify-content: center;
            align-items: flex-start;
            padding: 2rem;
            overflow-y: auto;
        }}

        .lightbox.active {{
            display: flex;
        }}

        .lightbox-content {{
            display: flex;
            flex-direction: column;
            max-width: 1200px;
            width: 100%;
            margin: auto;
        }}

        .lightbox-content img {{
            max-width: 100%;
            max-height: 60vh;
            object-fit: contain;
            border-radius: 4px;
            align-self: center;
        }}

        .lightbox-close {{
            position: fixed;
            top: 1rem;
            right: 1.5rem;
            font-size: 2rem;
            color: #fff;
            cursor: pointer;
            opacity: 0.7;
            background: none;
            border: none;
            z-index: 1001;
        }}

        .lightbox-close:hover {{
            opacity: 1;
        }}

        .lightbox-info {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem;
            background: #16213e;
            border-radius: 4px;
            margin-top: 1rem;
        }}

        .lightbox-title {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
        }}

        .lightbox-hardware {{
            display: block;
            font-size: 0.8rem;
            color: #888;
            margin-top: 0.25rem;
        }}

        .lightbox-meta {{
            display: flex;
            gap: 1rem;
            align-items: center;
        }}

        .lightbox-badge {{
            padding: 0.25rem 0.75rem;
            border-radius: 4px;
            font-size: 0.85rem;
            font-weight: 600;
            text-transform: uppercase;
        }}

        .lightbox-badge.pass {{
            background: rgba(0, 200, 83, 0.2);
            color: #69f0ae;
        }}

        .lightbox-badge.fail {{
            background: rgba(255, 82, 82, 0.2);
            color: #ff8a80;
        }}

        .lightbox-duration {{
            color: #888;
            font-size: 0.9rem;
        }}

        .lightbox-log {{
            background: #0f3460;
            border-radius: 4px;
            padding: 1rem;
            margin-top: 1rem;
            max-height: 300px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 0.8rem;
            line-height: 1.4;
            white-space: pre-wrap;
            word-break: break-all;
            color: #ccc;
        }}

        .lightbox-log::-webkit-scrollbar {{
            width: 8px;
        }}

        .lightbox-log::-webkit-scrollbar-track {{
            background: #16213e;
            border-radius: 4px;
        }}

        .lightbox-log::-webkit-scrollbar-thumb {{
            background: #4da6ff;
            border-radius: 4px;
        }}

        /* Video Player */
        .video-player {{
            display: none;
            margin-top: 1rem;
            background: #1a1a2e;
            border-radius: 8px;
            padding: 1rem;
        }}

        .video-player.active {{
            display: block;
        }}

        .video-controls {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-top: 0.5rem;
        }}

        .video-play-btn {{
            background: #4da6ff;
            color: white;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9rem;
        }}

        .video-play-btn:hover {{
            background: #3d8ee0;
        }}

        .video-frame-counter {{
            color: #888;
            font-size: 0.85rem;
        }}

        .video-slider-container {{
            padding: 15px 0;
        }}

        /* noUiSlider customization */
        #video-slider {{
            height: 6px;
            background: #333;
            border: none;
            box-shadow: none;
        }}

        #video-slider .noUi-connect {{
            background: #4da6ff;
        }}

        #video-slider .noUi-handle {{
            width: 16px;
            height: 16px;
            background: #4da6ff;
            border: none;
            border-radius: 50%;
            box-shadow: none;
            top: -5px;
            right: -8px;
            cursor: pointer;
        }}

        #video-slider .noUi-handle:before,
        #video-slider .noUi-handle:after {{
            display: none;
        }}

        /* Pips (tick marks) */
        .noUi-pips {{
            color: #666;
        }}

        .noUi-marker {{
            background: #4da6ff;
            width: 2px;
        }}

        .noUi-marker-large {{
            height: 12px;
        }}

        .noUi-value {{
            display: none;
        }}

        /* Footer */
        footer {{
            text-align: center;
            padding: 2rem;
            color: #666;
            font-size: 0.85rem;
        }}

        footer a {{
            color: #4da6ff;
            text-decoration: none;
        }}

        footer a:hover {{
            text-decoration: underline;
        }}

        /* Responsive */
        @media (max-width: 600px) {{
            .workflow-grid {{
                grid-template-columns: 1fr;
            }}

            .stats {{
                flex-direction: column;
                align-items: flex-start;
            }}
        }}
    </style>
    <link href="https://cdn.jsdelivr.net/npm/nouislider@15/dist/nouislider.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/nouislider@15/dist/nouislider.min.js"></script>
</head>
<body>
    <header>
        <h1><a href="https://github.com/PozzettiAndrea/{repo_name}">{repo_name}</a> Test Results</h1>
        <p class="meta">{timestamp_display}</p>
    </header>

    <div class="container">
        <div class="summary">
            <div class="progress-bar">
                <div class="progress-fill{' has-failures' if failed > 0 else ''}" style="width: {pass_rate}%"></div>
            </div>
            <div class="stats">
                <span class="stat-badge stat-pass">{passed} PASSED</span>
                {f'<span class="stat-badge stat-fail">{failed} FAILED</span>' if failed > 0 else ''}
                <span class="stat-total">{passed}/{total} tests ({pass_rate:.1f}%)</span>
            </div>
        </div>

        {failed_section}

        <h2 class="section-title">All Workflows</h2>
        <div class="workflow-grid">
            {workflow_cards}
        </div>
    </div>

    <div class="lightbox" id="lightbox">
        <button class="lightbox-close" onclick="closeLightbox()">&times;</button>
        <div class="lightbox-content">
            <img id="lightbox-img" src="" alt="">
            <div class="video-player" id="video-player">
                <div class="video-slider-container">
                    <div id="video-slider"></div>
                </div>
                <div class="video-controls">
                    <span class="video-frame-counter" id="video-frame-counter">0.0s / 0.0s</span>
                </div>
            </div>
            <div class="lightbox-info">
                <div>
                    <span class="lightbox-title" id="lightbox-title"></span>
                    <span class="lightbox-hardware" id="lightbox-hardware"></span>
                </div>
                <div class="lightbox-meta">
                    <span class="lightbox-badge" id="lightbox-badge"></span>
                    <span class="lightbox-duration" id="lightbox-duration"></span>
                </div>
            </div>
            <pre class="lightbox-log" id="lightbox-log"></pre>
        </div>
    </div>

    <footer>
        Generated by <a href="https://github.com/PozzettiAndrea/comfy-test">comfy-test</a>
    </footer>

    <script>
        // Store workflow data for hash-based linking
        const workflowData = {workflow_data_js};

        // Store video metadata (frames with timestamps and logs)
        const videoData = {video_data_js};

        // Video player state
        let currentWorkflow = null;
        let playInterval = null;
        let originalLogContent = '';

        // Current frame data for video player
        let currentFrameIndex = 0;
        let currentFrames = [];
        let currentTotalTime = 0;

        function openLightboxByName(name) {{
            const w = workflowData[name];
            if (w) openLightbox(w.src, w.title, w.status, w.duration, w.log, w.hardware);
        }}

        function openLightbox(src, title, status, duration, logContent, hardware) {{
            document.getElementById('lightbox-img').src = src;
            document.getElementById('lightbox-title').textContent = title;
            currentWorkflow = title;
            originalLogContent = logContent;

            const badge = document.getElementById('lightbox-badge');
            badge.textContent = status;
            badge.className = 'lightbox-badge ' + status;

            document.getElementById('lightbox-duration').textContent = duration + 's';
            document.getElementById('lightbox-log').textContent = logContent || '(No log available)';

            // Display hardware info
            const hwEl = document.getElementById('lightbox-hardware');
            if (hardware) {{
                const parts = [];
                if (hardware.os) parts.push(hardware.os);
                if (hardware.cpu) parts.push(hardware.cpu);
                if (hardware.gpu) parts.push(hardware.gpu);
                hwEl.textContent = parts.join(' | ');
            }} else {{
                hwEl.textContent = status === 'skipped' ? '(Did not run on this machine)' : '';
            }}

            // Setup video player if video data exists
            const data = videoData[title];
            const videoPlayer = document.getElementById('video-player');

            if (data && data.frames && data.frames.length > 1) {{
                currentFrames = data.frames;
                currentTotalTime = data.total_time || data.frames[data.frames.length - 1].time;
                setupVideoSlider(data.frames, currentTotalTime);
                updateVideoFrameByIndex(title, data.frames.length - 1);
                videoPlayer.classList.add('active');
            }} else {{
                videoPlayer.classList.remove('active');
            }}

            document.getElementById('lightbox').classList.add('active');
            history.replaceState(null, '', '#' + encodeURIComponent(title));
        }}

        function buildSliderRange(frames, totalTime) {{
            // Build noUiSlider range with frames at their time percentages
            const range = {{ 'min': frames[0].time }};
            frames.forEach((frame, idx) => {{
                if (idx > 0 && idx < frames.length - 1) {{
                    const pct = (frame.time / totalTime * 100).toFixed(1) + '%';
                    range[pct] = frame.time;
                }}
            }});
            range['max'] = frames[frames.length - 1].time;
            return range;
        }}

        function setupVideoSlider(frames, totalTime) {{
            const sliderEl = document.getElementById('video-slider');

            // Destroy existing slider if any
            if (sliderEl.noUiSlider) {{
                sliderEl.noUiSlider.destroy();
            }}

            const range = buildSliderRange(frames, totalTime);

            noUiSlider.create(sliderEl, {{
                start: frames[frames.length - 1].time,
                snap: true,
                range: range,
                pips: {{
                    mode: 'range',
                    density: 100
                }}
            }});

            sliderEl.noUiSlider.on('update', function(values) {{
                const time = parseFloat(values[0]);
                const frameIdx = frames.findIndex(f => Math.abs(f.time - time) < 0.01);
                if (frameIdx >= 0 && frameIdx !== currentFrameIndex) {{
                    currentFrameIndex = frameIdx;
                    showFrame(currentWorkflow, frameIdx);
                }}
            }});
        }}

        function showFrame(workflowName, frameIndex) {{
            const data = videoData[workflowName];
            if (!data || !data.frames) return;

            const frame = data.frames[frameIndex];
            const img = document.getElementById('lightbox-img');

            // Use high-quality screenshot for the last frame
            const isLastFrame = frameIndex === data.frames.length - 1;
            if (isLastFrame && workflowData[workflowName] && workflowData[workflowName].src) {{
                img.src = workflowData[workflowName].src;
            }} else {{
                img.src = 'videos/' + workflowName + '/' + frame.file;
            }}

            const counter = document.getElementById('video-frame-counter');
            const totalTime = data.total_time || data.frames[data.frames.length - 1].time;
            counter.textContent = frame.time.toFixed(1) + 's / ' + totalTime.toFixed(1) + 's';

            const logEl = document.getElementById('lightbox-log');
            if (frame.log) {{
                logEl.textContent = frame.log;
                logEl.scrollTop = logEl.scrollHeight;
            }}
        }}

        function updateVideoFrameByIndex(workflowName, frameIndex) {{
            currentFrameIndex = frameIndex;
            showFrame(workflowName, frameIndex);

            // Update slider position
            const sliderEl = document.getElementById('video-slider');
            if (sliderEl.noUiSlider && currentFrames[frameIndex]) {{
                sliderEl.noUiSlider.set(currentFrames[frameIndex].time);
            }}
        }}

        function toggleVideoPlay() {{
            const btn = document.getElementById('video-play-btn');
            if (playInterval) {{
                clearInterval(playInterval);
                playInterval = null;
                btn.innerHTML = '&#9654; Play';
            }} else {{
                btn.innerHTML = '&#9632; Stop';
                playInterval = setInterval(() => {{
                    if (!currentFrames.length) return;
                    currentFrameIndex = (currentFrameIndex + 1) % currentFrames.length;
                    updateVideoFrameByIndex(currentWorkflow, currentFrameIndex);
                }}, 500);
            }}
        }}

        function closeLightbox() {{
            // Stop video playback
            if (playInterval) {{
                clearInterval(playInterval);
                playInterval = null;
                document.getElementById('video-play-btn').innerHTML = '&#9654; Play';
            }}
            document.getElementById('lightbox').classList.remove('active');
            // Clear hash
            history.replaceState(null, '', window.location.pathname);
        }}

        document.getElementById('lightbox').onclick = (e) => {{
            if (e.target.id === 'lightbox') closeLightbox();
        }};

        document.onkeydown = (e) => {{
            if (e.key === 'Escape') closeLightbox();
        }};

        // Handle hash on page load and hash change
        function openFromHash() {{
            const hash = decodeURIComponent(window.location.hash.slice(1));
            if (hash && workflowData[hash]) {{
                const w = workflowData[hash];
                openLightbox(w.src, w.title, w.status, w.duration, w.log, w.hardware);
            }}
        }}

        window.addEventListener('hashchange', openFromHash);
        window.addEventListener('load', openFromHash);
    </script>
</body>
</html>'''


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
                    <span class="failed-name">{name}</span>
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
        hardware = w.get("hardware")  # Per-workflow hardware info
        screenshot_file = screenshots.get(name, "")
        log_content = log_contents.get(name, "")

        # Store data for hash-based linking
        src = f'screenshots/{screenshot_file}' if screenshot_file else ''
        workflow_data[name] = {
            'src': src,
            'title': name,
            'status': status,
            'duration': f'{duration:.2f}',
            'log': log_content,
            'hardware': hardware,
        }

        # Add failed class for red border
        failed_class = "failed" if status == "fail" else ""

        # Escape name for use in JavaScript string (single quotes)
        name_escaped = html.escape(name).replace("'", "\\'")

        # Screenshot or placeholder
        if screenshot_file:
            screenshot_html = f'''
                <img class="workflow-screenshot" src="screenshots/{screenshot_file}"
                     alt="{html.escape(name)}" loading="lazy">
            '''
            onclick = f"""onclick="openLightboxByName('{name_escaped}')\""""
            clickable = "clickable"
        else:
            screenshot_html = '<div class="workflow-screenshot placeholder">No screenshot</div>'
            # Still allow clicking to see log even without screenshot
            onclick = f"""onclick="openLightboxByName('{name_escaped}')\""""
            clickable = "clickable"

        cards.append(f'''
            <div class="workflow-card {clickable} {failed_class}" {onclick}>
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

    # Convert workflow data to JSON for JavaScript
    workflow_data_js = json.dumps(workflow_data)

    return '\n'.join(cards), workflow_data_js
