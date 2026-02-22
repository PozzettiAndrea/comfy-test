"""Workflow screenshot capture using headless browser."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import requests
from pathlib import Path
from typing import Optional, Callable, List, TYPE_CHECKING

try:
    from playwright.sync_api import sync_playwright, Page, Browser
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from PIL import Image, PngImagePlugin
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from ..common.errors import TestError, WorkflowError

if TYPE_CHECKING:
    from ..common.base_platform import TestPaths, TestPlatform
    from ..common.config import TestConfig


class ScreenshotError(TestError):
    """Error during screenshot capture."""
    pass


def _detect_gpu() -> bool:
    """Check if a GPU is available on this machine (independent of test mode)."""
    import shutil
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except Exception:
            pass
    return False


def _detect_vulkan() -> bool:
    """Check if Vulkan is available on this machine."""
    import shutil
    if shutil.which("vulkaninfo"):
        try:
            result = subprocess.run(
                ["vulkaninfo", "--summary"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and "deviceName" in result.stdout:
                return True
        except Exception:
            pass
    return False


def _detect_mesa_llvmpipe() -> bool:
    """Check if Mesa llvmpipe (software OpenGL) is available."""
    import platform
    if platform.system() == "Linux":
        # Check if EGL is available via Mesa
        for lib in ["/usr/lib/x86_64-linux-gnu/libEGL_mesa.so.0",
                    "/usr/lib/x86_64-linux-gnu/libEGL_mesa.so",
                    "/usr/lib64/libEGL_mesa.so.0"]:
            if os.path.exists(lib):
                return True
    elif platform.system() == "Windows":
        # Check if Mesa opengl32.dll has been placed next to Chromium
        # (handled by _ensure_mesa_windows)
        pass
    return False


def _ensure_mesa_linux(log: Callable[[str], None]) -> bool:
    """Install Mesa EGL/llvmpipe on Linux if not present."""
    if _detect_mesa_llvmpipe():
        return True
    log("  Installing Mesa llvmpipe (apt)...")
    try:
        subprocess.run(["sudo", "apt-get", "update", "-qq"], capture_output=True, timeout=60)
        result = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "-qq", "libegl1-mesa", "libegl-mesa0", "libgl1-mesa-dri"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            log("  Mesa llvmpipe installed")
            return True
        else:
            log(f"  Mesa install failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"  Mesa install error: {e}")
    return False


# Mesa DLL URL for Windows (x64, release MSVC build from mesa-dist-win)
_MESA_WIN_URL = "https://github.com/pal1000/mesa-dist-win/releases/download/25.3.6/mesa3d-25.3.6-release-msvc.7z"


def _ensure_mesa_windows(chromium_dir: Path, log: Callable[[str], None]) -> bool:
    """Download and place Mesa opengl32.dll next to Chromium on Windows."""
    opengl_dll = chromium_dir / "opengl32.dll"
    gallium_dll = chromium_dir / "libgallium_wgl.dll"
    if opengl_dll.exists() and gallium_dll.exists():
        log("  Mesa llvmpipe DLLs already present")
        return True

    log("  Downloading Mesa llvmpipe for Windows...")
    try:
        import urllib.request
        import shutil

        archive_path = chromium_dir / "mesa3d.7z"
        urllib.request.urlretrieve(_MESA_WIN_URL, str(archive_path))

        # Extract with 7z (available on Windows runners)
        extract_dir = chromium_dir / "_mesa_extract"
        result = subprocess.run(
            ["7z", "x", str(archive_path), f"-o{extract_dir}", "-y"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log(f"  7z extract failed: {result.stderr[:200]}")
            return False

        # Find and copy the x64 DLLs
        for root, dirs, files in os.walk(str(extract_dir)):
            root_path = Path(root)
            if "x64" in root_path.parts or root_path.name == "x64":
                for fname in ["opengl32.dll", "libgallium_wgl.dll"]:
                    src = root_path / fname
                    if src.exists():
                        shutil.copy2(str(src), str(chromium_dir / fname))
                        log(f"  Copied {fname} to Chromium dir")

        # Cleanup
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(str(extract_dir), ignore_errors=True)

        return opengl_dll.exists() and gallium_dll.exists()
    except Exception as e:
        log(f"  Mesa download error: {e}")
    return False


def check_dependencies() -> None:
    """Check that required dependencies are installed.

    Raises:
        ImportError: If playwright or PIL is not installed
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise ImportError(
            "Playwright is required for screenshots. "
            "Install it with: pip install comfy-test[screenshot]"
        )
    if not PIL_AVAILABLE:
        raise ImportError(
            "Pillow is required for screenshots. "
            "Install it with: pip install comfy-test[screenshot]"
        )


def _image_hash(img: "Image.Image") -> str:
    """Get hash of image for deduplication."""
    return hashlib.md5(img.tobytes()).hexdigest()


def _dedupe_frames(frames: List["Image.Image"]) -> List["Image.Image"]:
    """Remove consecutive duplicate frames."""
    if not frames:
        return []
    unique = []
    last_hash = None
    for frame in frames:
        h = _image_hash(frame)
        if h != last_hash:
            unique.append(frame)
            last_hash = h
    return unique


def _create_gif(
    frames: List["Image.Image"],
    output_path: Path,
    duration_ms: int = 500,
) -> None:
    """Create animated GIF from frames."""
    if not frames:
        return
    # Convert RGBA to RGB for GIF compatibility (GIF doesn't support alpha well)
    rgb_frames = []
    for frame in frames:
        if frame.mode == "RGBA":
            # Create white background and paste frame on it
            bg = Image.new("RGB", frame.size, (255, 255, 255))
            bg.paste(frame, mask=frame.split()[3])
            rgb_frames.append(bg)
        else:
            rgb_frames.append(frame.convert("RGB"))

    rgb_frames[0].save(
        output_path,
        save_all=True,
        append_images=rgb_frames[1:],
        duration=duration_ms,
        loop=0,  # Loop forever
    )


def ensure_dependencies(
    python_path: Optional[Path] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Ensure screenshot dependencies are installed, installing if needed.

    Automatically installs playwright and pillow if they are not available,
    then downloads the chromium browser for playwright.

    Args:
        python_path: Path to Python interpreter to install into.
                     If None, uses current interpreter.
        log_callback: Optional callback for logging messages.

    Returns:
        True if dependencies are available (or were successfully installed),
        False if installation failed.
    """
    global PLAYWRIGHT_AVAILABLE, PIL_AVAILABLE
    global sync_playwright, Page, Browser, Image, PngImagePlugin

    log = log_callback or (lambda msg: print(msg))

    # Check if already available
    if PLAYWRIGHT_AVAILABLE and PIL_AVAILABLE:
        return True

    log("Installing screenshot dependencies (playwright, pillow)...")

    try:
        # Try installing into the running process's environment first
        python = sys.executable
        result = subprocess.run(
            ["uv", "pip", "install", "--python", python, "playwright", "pillow"],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Current env not writable (e.g. system Python). Fall back to workspace venv.
            if python_path and str(python_path) != python:
                log(f"  Current env not writable, installing into workspace venv...")
                python = str(python_path)
                result = subprocess.run(
                    ["uv", "pip", "install", "--python", python, "playwright", "pillow"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log(f"  Failed to install packages: {result.stderr}")
                    return False
                # Add workspace venv site-packages to sys.path so we can import
                sp_result = subprocess.run(
                    [python, "-c", "import site; print(site.getsitepackages()[0])"],
                    capture_output=True, text=True,
                )
                if sp_result.returncode == 0:
                    site_pkg = sp_result.stdout.strip()
                    if site_pkg not in sys.path:
                        sys.path.insert(0, site_pkg)
            else:
                log(f"  Failed to install packages: {result.stderr}")
                return False

        log("  Packages installed, downloading chromium browser...")

        # Install chromium browser (required for playwright to work)
        result = subprocess.run(
            [python, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"  Failed to install chromium: {result.stderr}")
            return False

        log("  Screenshot dependencies installed successfully")

        # Update availability flags and import globals
        try:
            from playwright.sync_api import sync_playwright, Page, Browser
            PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            pass

        try:
            from PIL import Image, PngImagePlugin
            PIL_AVAILABLE = True
        except ImportError:
            pass

        return PLAYWRIGHT_AVAILABLE and PIL_AVAILABLE

    except Exception as e:
        log(f"  Error installing dependencies: {e}")
        return False


class WorkflowScreenshot:
    """Captures screenshots of ComfyUI workflows with embedded metadata.

    Uses Playwright to render workflows in a headless browser and captures
    screenshots of the graph canvas. The workflow JSON is embedded in the
    PNG metadata so the image can be dragged back into ComfyUI.

    Args:
        server_url: URL of a running ComfyUI server
        width: Viewport width (default: 1920)
        height: Viewport height (default: 1080)
        log_callback: Optional callback for logging

    Example:
        >>> with WorkflowScreenshot("http://127.0.0.1:8188") as ws:
        ...     ws.capture(Path("workflow.json"), Path("workflow.png"))
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8188",
        width: int = 1920,
        height: int = 1080,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        check_dependencies()

        self.server_url = server_url.rstrip("/")
        self.width = width
        self.height = height
        self._log = log_callback or (lambda msg: print(msg))
        self._playwright = None
        self._browser: Optional["Browser"] = None
        self._page: Optional["Page"] = None
        self._console_logs: List[str] = []


    def start(self) -> None:
        """Start the headless browser."""
        if self._browser is not None:
            return

        self._log("Starting headless browser...")
        self._playwright = sync_playwright().start()

        # Detect rendering backend: GPU > Mesa llvmpipe > SwiftShader
        import platform
        has_gpu = _detect_gpu()
        has_vulkan = _detect_vulkan() if has_gpu else False

        if has_gpu and has_vulkan:
            self._log("  GPU + Vulkan detected — using ANGLE/Vulkan rendering")
            chrome_args = [
                "--use-gl=angle",
                "--use-angle=vulkan",
                "--enable-features=Vulkan",
                "--disable-vulkan-surface",     # use bit blit for headless (no swapchain)
                "--ignore-gpu-blocklist",
                "--enable-unsafe-swiftshader",  # fallback if Vulkan fails
            ]
        elif has_gpu:
            self._log("  GPU detected (no Vulkan) — using ANGLE/OpenGL rendering")
            chrome_args = [
                "--use-gl=angle",
                "--ignore-gpu-blocklist",
                "--enable-unsafe-swiftshader",  # fallback if HW accel fails
            ]
        elif platform.system() == "Linux" and _ensure_mesa_linux(self._log):
            self._log("  Using Mesa llvmpipe (EGL) — multithreaded CPU rendering")
            chrome_args = ["--use-gl=egl", "--ignore-gpu-blocklist"]
        elif platform.system() == "Windows":
            # Get Chromium dir from Playwright to drop Mesa DLLs next to it
            chromium_path = Path(self._playwright.chromium.executable_path)
            chromium_dir = chromium_path.parent
            if _ensure_mesa_windows(chromium_dir, self._log):
                self._log("  Using Mesa llvmpipe (desktop GL) — multithreaded CPU rendering")
                chrome_args = ["--use-gl=desktop"]
            else:
                self._log("  Mesa unavailable — falling back to SwiftShader")
                chrome_args = ["--disable-gpu", "--use-gl=angle", "--use-angle=swiftshader"]
        else:
            self._log("  No GPU, no Mesa — using SwiftShader software rendering")
            chrome_args = ["--disable-gpu", "--use-gl=angle", "--use-angle=swiftshader"]

        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=chrome_args,
        )
        self._page = self._browser.new_page(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=2,  # HiDPI for crisp screenshots
        )
        # Increase default timeout for CI environments (macOS/WSL can be slow)
        screenshot_timeout = int(os.environ.get("COMFY_TEST_SCREENSHOT_TIMEOUT", "120000"))
        self._page.set_default_timeout(screenshot_timeout)
        # Capture browser console messages
        self._page.on("console", self._handle_console)

    def _handle_console(self, msg) -> None:
        """Capture browser console messages."""
        log_entry = f"[Console-{msg.type}] {msg.text}"
        self._console_logs.append(log_entry)
        # Only log errors and warnings to avoid noise
        if msg.type in ("error", "warning"):
            self._log(f"  {log_entry}")

    def save_console_logs(self, output_path: Path) -> None:
        """Save captured console logs to file."""
        if self._console_logs:
            output_path.write_text("\n".join(self._console_logs), encoding="utf-8")

    def clear_console_logs(self) -> None:
        """Clear captured console logs."""
        self._console_logs.clear()

    def _safe_screenshot(self, path: str, **kwargs) -> bool:
        """Take a screenshot, logging the full traceback on failure instead of crashing.

        Returns:
            True if screenshot succeeded, False if it failed.
        """
        try:
            self._page.screenshot(path=path, animations="disabled", **kwargs)
            return True
        except Exception:
            self._log(f"  WARNING: Screenshot failed ({path}):")
            self._log(traceback.format_exc())
            return False

    def _screenshot_with_retry(self, path: str, retries: int = 3, **kwargs) -> None:
        """Take screenshot with retry logic for flaky CI environments.

        Args:
            path: Path to save screenshot
            retries: Number of retry attempts (default 3)
            **kwargs: Additional arguments passed to page.screenshot()

        Raises:
            ScreenshotError: If all retry attempts fail
        """
        last_error = None
        for attempt in range(retries):
            try:
                self._page.screenshot(path=path, animations="disabled", **kwargs)
                return
            except Exception as e:
                last_error = e
                self._log(f"  Screenshot attempt {attempt + 1}/{retries} failed: {e}")
                self._log(traceback.format_exc())
                if attempt < retries - 1:
                    self._page.wait_for_timeout(1000)  # Wait before retry
        raise ScreenshotError(f"Screenshot failed after {retries} attempts", str(last_error))

    def stop(self) -> None:
        """Stop the headless browser."""
        if self._page:
            self._page.close()
            self._page = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def _configure_server_settings(self) -> None:
        """Set server-side settings for screenshot capture."""
        settings = {
            # Prevent Templates panel from showing on first run
            "Comfy.TutorialCompleted": True,
            # Enable Vue node overlays so preview images render as DOM elements
            # (defaults to false on fresh installs, which prevents all preview rendering)
            "Comfy.VueNodes.Enabled": True,
        }
        for key, value in settings.items():
            try:
                requests.post(
                    f"{self.server_url}/settings/{key}",
                    json=value,
                    timeout=5,
                )
            except Exception:
                pass  # Best effort - server might not be running yet

    def _disable_first_run_tutorial(self) -> None:
        """Set server-side setting to prevent Templates panel from showing.

        Deprecated: use _configure_server_settings() instead.
        """
        self._configure_server_settings()

    def _close_panels_and_alerts(self) -> None:
        """Close Templates sidebar panel if open."""
        try:
            # Click the X button (pi-times icon) on Templates panel
            self._page.evaluate("""
                (() => {
                    const closeIcon = document.querySelector('i.pi.pi-times');
                    if (closeIcon) closeIcon.click();
                })();
            """)
            self._page.wait_for_timeout(200)
        except Exception:
            pass

    def _fit_graph_to_view(self) -> None:
        """Fit the entire graph/workflow in the viewport.

        Uses the '.' keyboard shortcut which triggers ComfyUI's built-in
        "Fit view to selection (whole graph when nothing is selected)" feature.
        """
        try:
            # Press '.' to trigger fit view (ComfyUI keyboard shortcut)
            self._page.keyboard.press(".")
            self._page.wait_for_timeout(500)
        except Exception:
            pass  # Best effort

    def _trigger_3d_previews(self) -> None:
        """Activate Load3d/Preview3D nodes so their Three.js loop renders.

        ComfyUI's Load3d widget skips rendering unless isActive() is true.
        We find all such nodes and dispatch mouseenter on their DOM widgets
        to set STATUS_MOUSE_ON_SCENE = true.

        Does NOT unfreeze rAF — 3D viewers already rendered during execution
        and their canvases retain the last frame.  Unfreezing would restart
        heavy SwiftShader renders (e.g. 1.18M gaussians) causing 55s+ stalls.
        """
        try:
            self._page.evaluate("""
                (() => {
                    const nodes = window.app.graph._nodes || [];
                    for (const node of nodes) {
                        const t = (node.type || '').toLowerCase();
                        if (!t.includes('3d') && !t.includes('load3d') && !t.includes('preview3d') && !t.includes('gaussian')) continue;

                        if (node.widgets) {
                            for (const widget of node.widgets) {
                                const el = widget.element || widget.inputEl;
                                if (el) {
                                    el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                                }
                            }
                        }

                        if (typeof node.onMouseEnter === 'function') {
                            try { node.onMouseEnter({}); } catch(e) {}
                        }
                    }
                })();
            """)
        except Exception:
            pass  # Best effort

    def _freeze_animations(self) -> None:
        """Freeze all requestAnimationFrame loops to unblock the compositor.

        Three.js and other WebGL renderers use rAF loops that keep the
        browser compositor busy via SwiftShader, which can cause
        page.screenshot() to hang indefinitely on CPU-only machines.

        This replaces rAF with a no-op so no new render calls are queued.
        The currently in-flight render (if any) finishes normally, and the
        canvas retains its last rendered frame for the screenshot.

        Also freezes iframes (3D viewers like gaussian splat run in iframes
        with their own window and rAF loop).

        """
        try:
            self._page.evaluate("""
                (() => {
                    // Stop all rAF-based render loops (Three.js, gaussian viewers, etc.)
                    if (!window._origRAF) {
                        window._origRAF = window.requestAnimationFrame;
                    }
                    window.requestAnimationFrame = () => 0;

                    // Also cancel any already-queued frames
                    for (let i = 1; i < 10000; i++) {
                        window.cancelAnimationFrame(i);
                    }

                    // Freeze iframes too (3D viewers run in iframes)
                    // Unlike the main window, we capture callbacks instead of
                    // discarding them so the render loop can be restarted on
                    // unfreeze.  We also skip cancelAnimationFrame — letting
                    // the one already-queued callback fire ensures it calls
                    // our replacement rAF, which saves the loop function.
                    document.querySelectorAll('iframe').forEach(iframe => {
                        try {
                            const win = iframe.contentWindow;
                            if (!win) return;
                            if (!win._origRAF) win._origRAF = win.requestAnimationFrame;
                            win._pendingRAFCallbacks = [];
                            win.requestAnimationFrame = (cb) => {
                                win._pendingRAFCallbacks.push(cb);
                                return 0;
                            };
                        } catch(e) {} // Cross-origin iframes will throw
                    });
                })();
            """)
            # Wait for any in-flight SwiftShader render to complete
            self._page.wait_for_timeout(2000)
        except Exception:
            pass  # Best effort

    def _unfreeze_animations(self) -> None:
        """Restore requestAnimationFrame after a freeze."""
        try:
            self._page.evaluate("""
                (() => {
                    if (window._origRAF) {
                        window.requestAnimationFrame = window._origRAF;
                        delete window._origRAF;
                    }

                    // Restore iframes and restart any captured render loops
                    document.querySelectorAll('iframe').forEach(iframe => {
                        try {
                            const win = iframe.contentWindow;
                            if (win && win._origRAF) {
                                win.requestAnimationFrame = win._origRAF;
                                delete win._origRAF;
                                const pending = win._pendingRAFCallbacks || [];
                                win._pendingRAFCallbacks = [];
                                for (const cb of pending) {
                                    win.requestAnimationFrame(cb);
                                }
                            }
                        } catch(e) {}
                    });
                })();
            """)
        except Exception:
            pass

    def _validate_workflow_in_browser(self) -> dict:
        """Validate workflow using browser's graphToPrompt() conversion.

        Must be called after workflow is loaded into browser via loadGraphData().
        Uses graphToPrompt() for consistent conversion - this ensures we validate
        using the exact same API format that queuePrompt() will use.

        Returns:
            Dict with 'success' bool and optional 'node_errors' dict.

        Raises:
            ScreenshotError: If workflow validation hard-fails (HTTP error, missing class_type)
        """
        result = self._page.evaluate("""
            async () => {
                try {
                    // Get API format using browser's converter
                    const { output } = await window.app.graphToPrompt();

                    // Diagnostic: check conversion quality
                    const nodeIds = Object.keys(output || {});
                    const nodesWithoutClassType = nodeIds.filter(id => !output[id] || !output[id].class_type);
                    const sample = nodeIds.length > 0 ? JSON.stringify(output[nodeIds[0]]).substring(0, 300) : 'empty';
                    const diag = `graphToPrompt: ${nodeIds.length} nodes, missing class_type: [${nodesWithoutClassType.join(',')}], sample: ${sample}`;
                    console.log('[comfy-test] ' + diag);

                    if (nodesWithoutClassType.length > 0) {
                        return {
                            success: false,
                            error: {
                                message: 'graphToPrompt produced nodes without class_type: ' + diag
                            }
                        };
                    }

                    // Validate via /validate endpoint
                    const validateResp = await fetch('/validate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt: output })
                    });

                    // Get response as text first to debug any parsing issues
                    const responseText = await validateResp.text();

                    // Try to parse it
                    let data;
                    try {
                        data = JSON.parse(responseText);
                    } catch (parseErr) {
                        // Include raw response in error for debugging
                        const preview = responseText.substring(0, 200);
                        const hexPreview = Array.from(responseText.substring(0, 10))
                            .map(c => c.charCodeAt(0).toString(16).padStart(2, '0'))
                            .join(' ');
                        return {
                            success: false,
                            error: {
                                message: parseErr.toString() +
                                    ' | Response preview: ' + preview +
                                    ' | Hex: ' + hexPreview +
                                    ' | Length: ' + responseText.length
                            }
                        };
                    }

                    if (!validateResp.ok) {
                        return { success: false, error: data.error, node_errors: data.node_errors };
                    }

                    // Pass through node_errors even when valid (caller decides what to do)
                    if (data.node_errors && Object.keys(data.node_errors).length > 0) {
                        return { success: true, node_errors: data.node_errors };
                    }

                    return { success: true };
                } catch (e) {
                    return { success: false, error: { message: e.toString() } };
                }
            }
        """)

        if not result.get("success"):
            error = result.get("error", {})
            error_msg = error.get("message", "Unknown error")
            node_errors = result.get("node_errors")
            details = error_msg
            if node_errors:
                details += f"\nNode errors:\n{json.dumps(node_errors, indent=2)}"
            self._log(f"  Validation failed: {details}")
            raise ScreenshotError("Workflow validation failed", details)

        return result

    def __enter__(self) -> "WorkflowScreenshot":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    def validate_workflow(self, workflow_path: Path) -> None:
        """Validate a workflow without executing it.

        Loads the workflow into the browser and validates via /validate endpoint.
        This checks that all nodes can be instantiated with their inputs.

        Args:
            workflow_path: Path to the workflow JSON file

        Raises:
            ScreenshotError: If workflow validation fails
        """
        if self._page is None:
            raise ScreenshotError("Browser not started. Call start() or use context manager.")

        # Load workflow JSON
        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        # Configure server-side settings (tutorial, Vue node overlays for previews)
        self._configure_server_settings()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=300000,
            )
        except Exception as e:
            raise ScreenshotError("ComfyUI app did not initialize", str(e))

        # Load the workflow via JavaScript
        workflow_json = json.dumps(workflow)
        try:
            self._page.evaluate(f"""
                (async () => {{
                    const workflow = {workflow_json};
                    await window.app.loadGraphData(workflow);
                }})();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to load workflow into ComfyUI", str(e))

        # Wait for graph to render
        self._page.wait_for_timeout(1000)

        # Validate using browser's graphToPrompt() conversion
        self._validate_workflow_in_browser()

    def capture(
        self,
        workflow_path: Path,
        output_path: Optional[Path] = None,
        wait_ms: int = 2000,
    ) -> Path:
        """Capture a screenshot of a workflow.

        Args:
            workflow_path: Path to the workflow JSON file
            output_path: Path to save the PNG (default: same as workflow with .png extension)
            wait_ms: Time to wait after loading for graph to render (default: 2000ms)

        Returns:
            Path to the saved screenshot

        Raises:
            ScreenshotError: If capture fails
        """
        if self._page is None:
            raise ScreenshotError("Browser not started. Call start() or use context manager.")

        # Determine output path
        if output_path is None:
            output_path = workflow_path.with_suffix(".png")

        # Load workflow JSON
        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Capturing: {workflow_path.name}")

        # Configure server-side settings (tutorial, Vue node overlays for previews)
        self._configure_server_settings()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=300000,
            )
        except Exception as e:
            raise ScreenshotError("ComfyUI app did not initialize", str(e))

        # Load the workflow via JavaScript
        workflow_json = json.dumps(workflow)
        try:
            self._page.evaluate(f"""
                (async () => {{
                    const workflow = {workflow_json};
                    await window.app.loadGraphData(workflow);
                }})();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to load workflow into ComfyUI", str(e))

        # Wait for graph to render
        self._page.wait_for_timeout(wait_ms)

        # Fit the entire graph in view
        self._fit_graph_to_view()

        # Close any open panels (Templates sidebar) and dismiss alerts
        self._close_panels_and_alerts()

        # Take screenshot with a temp file first
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Full viewport screenshot (1920x1080 at 2x scale)
            self._screenshot_with_retry(path=str(tmp_path))

            # Embed workflow metadata into PNG
            self._embed_workflow(tmp_path, output_path, workflow)

        finally:
            # Clean up temp file
            if tmp_path.exists():
                tmp_path.unlink()

        self._log(f"  Saved: {output_path}")
        return output_path

    def capture_after_execution(
        self,
        workflow_path: Path,
        output_path: Optional[Path] = None,
        timeout: int = 3600,
        wait_after_completion_ms: int = 3000,
    ) -> Path:
        """Capture a screenshot after executing a workflow.

        Unlike capture(), this method actually executes the workflow and waits
        for it to complete before taking a screenshot. This shows the preview
        nodes with their actual rendered outputs (images, meshes, etc.).

        Args:
            workflow_path: Path to the workflow JSON file
            output_path: Path to save the PNG (default: workflow with _executed.png suffix)
            timeout: Max seconds to wait for execution to complete (default: 300)
            wait_after_completion_ms: Time to wait after completion for previews to render (default: 3000ms)

        Returns:
            Path to the saved screenshot

        Raises:
            ScreenshotError: If capture or execution fails
        """
        if self._page is None:
            raise ScreenshotError("Browser not started. Call start() or use context manager.")

        # Determine output path - use _executed suffix to distinguish from static screenshots
        if output_path is None:
            output_path = workflow_path.with_stem(workflow_path.stem + "_executed").with_suffix(".png")

        # Load workflow JSON
        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Executing and capturing: {workflow_path.name}")

        # Configure server-side settings (tutorial, Vue node overlays for previews)
        self._configure_server_settings()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=300000,
            )
        except Exception as e:
            raise ScreenshotError("ComfyUI app did not initialize", str(e))

        # Load the workflow via JavaScript
        workflow_json = json.dumps(workflow)
        try:
            self._page.evaluate(f"""
                (async () => {{
                    const workflow = {workflow_json};
                    await window.app.loadGraphData(workflow);
                }})();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to load workflow into ComfyUI", str(e))

        # Wait for graph to render before execution
        self._page.wait_for_timeout(2000)

        # Inject WebSocket listener to track execution completion
        self._page.evaluate("""
            window._executionComplete = false;
            window._executionError = null;

            if (window.app && window.app.api && window.app.api.socket) {
                const origOnMessage = window.app.api.socket.onmessage;
                window.app.api.socket.onmessage = function(event) {
                    if (origOnMessage) {
                        try { origOnMessage.call(this, event); } catch(e) {}
                    }
                    if (event && typeof event.data === 'string') {
                        try {
                            const msg = JSON.parse(event.data);
                            if (msg && msg.type === 'execution_success') {
                                window._executionComplete = true;
                            } else if (msg && msg.type === 'execution_error') {
                                window._executionError = msg.data;
                                window._executionComplete = true;
                            } else if (msg && msg.type === 'execution_interrupted') {
                                window._executionError = 'Execution interrupted';
                                window._executionComplete = true;
                            }
                        } catch (e) {}
                    }
                };
            }
        """)

        # Validate workflow using browser's graphToPrompt() conversion
        self._log("  Validating workflow...")
        self._validate_workflow_in_browser()

        # Queue using queuePrompt for proper WebSocket handling
        self._log("  Queuing workflow for execution...")
        self._page.evaluate("window.app.queuePrompt(0)")

        # Wait for WebSocket execution_success/error message
        self._log("  Waiting for execution to complete...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            complete = self._page.evaluate("window._executionComplete")
            if complete:
                break
            self._page.wait_for_timeout(500)
        else:
            self._log("  Warning: Timeout waiting for execution, proceeding anyway")

        # Extra wait for previews to fully render
        self._page.wait_for_timeout(wait_after_completion_ms)
        self._log("  Execution completed")

        # Fit the entire graph in view
        self._fit_graph_to_view()

        # Close any open panels (Templates sidebar) and dismiss alerts
        self._close_panels_and_alerts()

        # Activate Load3d/Preview3D nodes so their Three.js viewports render
        self._trigger_3d_previews()

        # Freeze rAF loops and WebGL contexts so the compositor can
        # produce a frame without SwiftShader blocking indefinitely
        self._freeze_animations()

        # Take screenshot with a temp file first
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Full viewport screenshot (1920x1080 at 2x scale)
            self._screenshot_with_retry(path=str(tmp_path))

            # Embed workflow metadata into PNG
            self._embed_workflow(tmp_path, output_path, workflow)

        finally:
            # Clean up temp file
            if tmp_path.exists():
                tmp_path.unlink()

        self._log(f"  Saved: {output_path}")
        return output_path

    def _embed_workflow(
        self,
        source_path: Path,
        output_path: Path,
        workflow: dict,
    ) -> None:
        """Embed workflow JSON into PNG metadata.

        Uses the same format as ComfyUI's "Save (embed workflow)" feature,
        so the resulting PNG can be dragged back into ComfyUI.

        Args:
            source_path: Path to the source PNG
            output_path: Path to save the PNG with metadata
            workflow: Workflow dictionary to embed
        """
        img = Image.open(source_path)

        # Create PNG metadata
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("workflow", json.dumps(workflow))

        # If workflow has "prompt" format (API format), also embed that
        if "nodes" not in workflow and all(k.isdigit() for k in workflow.keys()):
            # This looks like API format (prompt)
            pnginfo.add_text("prompt", json.dumps(workflow))

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Save with metadata
        img.save(output_path, pnginfo=pnginfo)
        img.close()

    def capture_execution_gif(
        self,
        workflow_path: Path,
        output_path: Optional[Path] = None,
        timeout: int = 3600,
        frame_duration_ms: int = 500,
    ) -> Path:
        """Capture workflow execution as animated GIF.

        Takes a screenshot after each node completes, then combines
        unique frames into an animated GIF.

        Args:
            workflow_path: Path to the workflow JSON file
            output_path: Path to save the GIF (default: workflow with _execution.gif suffix)
            timeout: Max seconds to wait for execution to complete (default: 300)
            frame_duration_ms: Duration of each frame in the GIF (default: 500ms)

        Returns:
            Path to the saved GIF

        Raises:
            ScreenshotError: If capture or execution fails
        """
        if self._page is None:
            raise ScreenshotError("Browser not started. Call start() or use context manager.")

        # Determine output path
        if output_path is None:
            output_path = workflow_path.with_stem(workflow_path.stem + "_execution").with_suffix(".gif")

        # Load workflow JSON
        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Capturing execution GIF: {workflow_path.name}")

        # Configure server-side settings (tutorial, Vue node overlays for previews)
        self._configure_server_settings()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=300000,
            )
        except Exception as e:
            raise ScreenshotError("ComfyUI app did not initialize", str(e))

        # Load the workflow via JavaScript
        workflow_json = json.dumps(workflow)
        try:
            self._page.evaluate(f"""
                (async () => {{
                    const workflow = {workflow_json};
                    await window.app.loadGraphData(workflow);
                }})();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to load workflow into ComfyUI", str(e))

        # Wait for graph to render before execution
        self._page.wait_for_timeout(2000)

        # Close any open panels before capturing
        self._close_panels_and_alerts()

        # Fit the entire graph in view
        self._fit_graph_to_view()

        # Wait for WebSocket to be ready
        try:
            self._page.wait_for_function(
                "window.app && window.app.api && window.app.api.socket && window.app.api.socket.readyState === 1",
                timeout=10000,
            )
        except Exception:
            self._log("  Warning: WebSocket not ready, proceeding anyway")

        # Inject WebSocket listener to track node execution
        # Capture on BOTH 'executing' (green box) and 'executed' (output ready)
        self._page.evaluate("""
            window._nodeEvents = [];
            window._executionComplete = false;
            window._executionError = null;

            if (window.app && window.app.api && window.app.api.socket) {
                const origOnMessage = window.app.api.socket.onmessage;
                window.app.api.socket.onmessage = function(event) {
                    if (origOnMessage) {
                        try { origOnMessage.call(this, event); } catch(e) {}
                    }
                    if (event && typeof event.data === 'string') {
                        try {
                            const msg = JSON.parse(event.data);
                            if (msg && msg.type === 'executing' && msg.data && msg.data.node) {
                                // Node starting - green highlight appears
                                window._nodeEvents.push({
                                    type: 'executing',
                                    node: msg.data.node,
                                    time: Date.now()
                                });
                            } else if (msg && msg.type === 'executed' && msg.data) {
                                // Node finished - output ready
                                window._nodeEvents.push({
                                    type: 'executed',
                                    node: msg.data.node,
                                    time: Date.now()
                                });
                            } else if (msg && msg.type === 'execution_success') {
                                window._executionComplete = true;
                            } else if (msg && msg.type === 'execution_error') {
                                window._executionError = msg.data;
                                window._executionComplete = true;
                            }
                        } catch (e) {}
                    }
                };
            }
        """)

        # Create temp directory for frames
        temp_dir = Path(tempfile.mkdtemp(prefix="comfy-gif-"))
        frames = []

        try:
            # Take initial frame (before execution)
            initial_frame = temp_dir / "frame_000.png"
            if self._safe_screenshot(path=str(initial_frame)):
                frames.append(Image.open(initial_frame))

            # Validate workflow using browser's graphToPrompt() conversion
            self._log("  Validating workflow...")
            self._validate_workflow_in_browser()

            # Queue using queuePrompt for proper WebSocket handling
            self._log("  Queuing workflow for execution...")
            self._page.evaluate("window.app.queuePrompt(0)")

            # Capture loop - periodic screenshots to catch green execution boxes
            # We take screenshots frequently during execution to catch the green highlights
            # Deduplication will remove identical frames later
            start_time = time.time()
            last_screenshot_time = 0
            frame_num = 1
            screenshot_interval_ms = 50  # Capture every 50ms to catch fast nodes

            while time.time() - start_time < timeout:
                current_time = time.time()

                # Check execution state
                state = self._page.evaluate("""
                    () => ({
                        complete: window._executionComplete,
                        error: window._executionError
                    })
                """)

                # Take periodic screenshot to catch execution state (green boxes)
                if (current_time - last_screenshot_time) * 1000 >= screenshot_interval_ms:
                    frame_path = temp_dir / f"frame_{frame_num:03d}.png"
                    if self._safe_screenshot(path=str(frame_path)):
                        frames.append(Image.open(frame_path))
                        frame_num += 1
                    last_screenshot_time = current_time

                if state["complete"]:
                    if state["error"]:
                        error_data = state["error"]
                        if isinstance(error_data, dict):
                            error_msg = error_data.get("message", str(error_data))
                            node_error = error_data.get("node_type")
                        else:
                            error_msg = str(error_data)
                            node_error = None
                        self._log(f"  Execution error: {error_msg}")
                        raise WorkflowError(f"Workflow execution failed: {error_msg}", workflow_file=str(workflow_path), node_error=node_error)
                    break

                self._page.wait_for_timeout(10)

            self._log(f"  Captured {frame_num - 1} frames during execution")

            # Final frame after completion
            self._page.wait_for_timeout(1000)  # Wait for final renders
            final_frame = temp_dir / f"frame_{frame_num:03d}.png"
            if self._safe_screenshot(path=str(final_frame)):
                frames.append(Image.open(final_frame))

            self._log(f"  Captured {len(frames)} total frames")

            # Dedupe frames
            unique_frames = _dedupe_frames(frames)
            self._log(f"  {len(unique_frames)} unique frames after deduplication")

            # Create GIF
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _create_gif(unique_frames, output_path, frame_duration_ms)

            self._log(f"  Saved: {output_path}")
            return output_path

        finally:
            # Clean up temp directory
            for frame in frames:
                frame.close()
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def capture_execution_frames(
        self,
        workflow_path: Path,
        output_dir: Path,
        webp_quality: int = 60,
        log_lines: Optional[List[str]] = None,
        final_screenshot_path: Optional[Path] = None,
        final_screenshot_delay_ms: int = 5000,
        timeout: int = 3600,
    ) -> List[Path]:
        """Capture workflow execution as frames triggered by node execution events.

        Takes a screenshot each time a node finishes executing (via WebSocket
        'executed' events), producing one frame per node instead of continuous
        polling. Also saves metadata.json with timestamps and log snapshots.

        Optionally captures a high-quality PNG screenshot after execution completes,
        waiting for previews to fully render.

        Args:
            workflow_path: Path to the workflow JSON file
            output_dir: Directory to save frames (e.g., videos/workflow_name/)
            webp_quality: JPEG compression quality 0-100 (default: 60)
            log_lines: Optional list that accumulates log lines (for syncing logs to frames)
            final_screenshot_path: Optional path to save high-quality PNG after execution
            final_screenshot_delay_ms: Delay before final screenshot (default: 5000ms)

        Returns:
            List of paths to saved JPEG frames

        Raises:
            ScreenshotError: If capture or execution fails
        """
        if self._page is None:
            raise ScreenshotError("Browser not started. Call start() or use context manager.")

        # Load workflow JSON
        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Capturing execution frames: {workflow_path.name}")

        # Clear execution cache to prevent state accumulation between workflows
        try:
            requests.post(
                f"{self.server_url}/free",
                json={"unload_models": False, "free_memory": True},
                timeout=5,
            )
        except Exception:
            pass  # Best effort

        # Configure server-side settings (tutorial, Vue node overlays for previews)
        self._configure_server_settings()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=300000,
            )
        except Exception as e:
            raise ScreenshotError("ComfyUI app did not initialize", str(e))

        # Load the workflow via JavaScript
        workflow_json = json.dumps(workflow)
        try:
            self._page.evaluate(f"""
                (async () => {{
                    const workflow = {workflow_json};
                    await window.app.loadGraphData(workflow);
                }})();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to load workflow into ComfyUI", str(e))

        # Wait for graph to render before execution
        self._page.wait_for_timeout(2000)

        # Close any open panels before capturing
        self._close_panels_and_alerts()

        # Fit the entire graph in view
        self._fit_graph_to_view()

        # Wait for WebSocket to be ready
        try:
            self._page.wait_for_function(
                "window.app && window.app.api && window.app.api.socket && window.app.api.socket.readyState === 1",
                timeout=10000,
            )
        except Exception:
            self._log("  Warning: WebSocket not ready, proceeding anyway")

        # Inject WebSocket listener to track execution completion and per-node events
        self._page.evaluate("""
            window._executionComplete = false;
            window._executionError = null;
            window._executedNodeCount = 0;
            window._executingNodeCount = 0;
            window._executedOutputs = {};

            if (window.app && window.app.api && window.app.api.socket) {
                const origOnMessage = window.app.api.socket.onmessage;
                window.app.api.socket.onmessage = function(event) {
                    if (origOnMessage) {
                        try { origOnMessage.call(this, event); } catch(e) {}
                    }
                    if (event && typeof event.data === 'string') {
                        try {
                            const msg = JSON.parse(event.data);
                            if (msg && msg.type === 'executing' && msg.data && msg.data.node) {
                                window._executingNodeCount++;
                            } else if (msg && msg.type === 'executed' && msg.data && msg.data.node) {
                                window._executedNodeCount++;
                                window._executedOutputs[msg.data.node] = msg.data.output;
                            } else if (msg && msg.type === 'execution_success') {
                                window._executionComplete = true;
                            } else if (msg && msg.type === 'execution_error') {
                                window._executionError = msg.data;
                                window._executionComplete = true;
                            } else if (msg && msg.type === 'execution_interrupted') {
                                window._executionError = 'Execution interrupted';
                                window._executionComplete = true;
                            }
                        } catch (e) {}
                    }
                };
            }

            // Diagnostic: hook into the frontend's own CustomEvent dispatch chain
            // to see if 'executed' events reach the app.api EventTarget
            window._diagApiExecuted = [];
            window._diagApiExecuting = [];
            if (window.app && window.app.api) {
                window.app.api.addEventListener('executed', (e) => {
                    window._diagApiExecuted.push({
                        node: e.detail?.node,
                        display_node: e.detail?.display_node,
                        imageCount: e.detail?.output?.images?.length || 0
                    });
                });
                window.app.api.addEventListener('executing', (e) => {
                    window._diagApiExecuting.push(e.detail);
                });
            }
        """)

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_paths: List[Path] = []
        frame_metadata = []
        last_hash = None
        frame_num = 0

        def _save_frame_if_new(screenshot_bytes: bytes, timestamp: float, log_snap: str) -> bool:
            """Hash screenshot, save as JPEG if different from last frame."""
            nonlocal last_hash, frame_num
            h = hashlib.md5(screenshot_bytes).hexdigest()
            if h == last_hash:
                return False
            last_hash = h
            jpg_path = output_dir / f"frame_{frame_num:03d}.jpg"
            img = Image.open(io.BytesIO(screenshot_bytes))
            img.save(jpg_path, "JPEG", quality=webp_quality)
            img.close()
            frame_paths.append(jpg_path)
            frame_metadata.append({
                "file": jpg_path.name,
                "time": timestamp,
                "log": log_snap,
            })
            frame_num += 1
            self._log(f"  Frame {frame_num} saved ({jpg_path.name}, t={timestamp:.1f}s)")
            return True

        # Quick rAF freeze/unfreeze for periodic captures (no sleep).
        # IMPORTANT: iframe callbacks must be SAVED, not discarded — otherwise
        # render loops (e.g. gaussian splat viewer) die permanently.
        _QUICK_FREEZE_JS = """(() => {
            window._origRAF = window._origRAF || window.requestAnimationFrame;
            window.requestAnimationFrame = () => 0;
            document.querySelectorAll('iframe').forEach(f => {
                try {
                    const w = f.contentWindow;
                    if (!w) return;
                    w._origRAF = w._origRAF || w.requestAnimationFrame;
                    w._pendingRAFCallbacks = w._pendingRAFCallbacks || [];
                    w.requestAnimationFrame = (cb) => { w._pendingRAFCallbacks.push(cb); return 0; };
                } catch(e) {}
            });
        })()"""
        _QUICK_UNFREEZE_JS = """(() => {
            if (window._origRAF) { window.requestAnimationFrame = window._origRAF; delete window._origRAF; }
            document.querySelectorAll('iframe').forEach(f => {
                try {
                    const w = f.contentWindow;
                    if (!w || !w._origRAF) return;
                    w.requestAnimationFrame = w._origRAF;
                    delete w._origRAF;
                    const pending = w._pendingRAFCallbacks || [];
                    w._pendingRAFCallbacks = [];
                    for (const cb of pending) { w.requestAnimationFrame(cb); }
                } catch(e) {}
            });
        })()"""

        try:
            capture_start = time.time()

            # Take initial frame (before execution)
            try:
                shot = self._page.screenshot(type="png", animations="disabled", scale="css")
                log_snapshot = "\n".join(log_lines) if log_lines else ""
                _save_frame_if_new(shot, 0.0, log_snapshot)
            except Exception:
                pass

            # Validate workflow using browser's graphToPrompt() conversion
            self._log("  Validating workflow...")
            validation = self._validate_workflow_in_browser()
            node_errors = validation.get("node_errors")
            if node_errors:
                self._log(f"  Warning: {len(node_errors)} node(s) have validation errors (will fail after screenshots)")

            # Queue using queuePrompt for proper WebSocket handling
            self._log("  Queuing workflow for execution...")
            self._page.evaluate("window.app.queuePrompt(0)")

            # Capture loop — screenshot on node events + periodic every 2s
            last_executed_count = 0
            last_screenshot_time = time.time()
            PERIODIC_INTERVAL = 2.0

            loop_iter = 0
            while time.time() - capture_start < timeout:
                loop_iter += 1
                t0 = time.time()
                state = self._page.evaluate("""
                    () => ({
                        complete: window._executionComplete,
                        error: window._executionError,
                        executedCount: window._executedNodeCount
                    })
                """)
                t_eval = time.time() - t0

                elapsed = time.time() - capture_start
                log_snapshot = "\n".join(log_lines) if log_lines else ""

                # Debug: log every 10th iteration or when something interesting happens
                if loop_iter % 50 == 0:
                    self._log(f"  [capture-loop] iter={loop_iter} t={elapsed:.1f}s state={state} eval_ms={t_eval*1000:.0f}")

                # Screenshot on node completion (with freeze to avoid GPU stalls)
                if state["executedCount"] > last_executed_count:
                    self._log(f"  Node executed ({state['executedCount']} total), capturing...")
                    self._page.wait_for_timeout(150)  # let UI render
                    elapsed = time.time() - capture_start
                    try:
                        t1 = time.time()
                        self._page.evaluate(_QUICK_FREEZE_JS)
                        t_freeze = time.time() - t1
                        t1 = time.time()
                        shot = self._page.screenshot(type="png", animations="disabled", scale="css")
                        t_shot = time.time() - t1
                        if not state["complete"]:
                            t1 = time.time()
                            self._page.evaluate(_QUICK_UNFREEZE_JS)
                            t_unfreeze = time.time() - t1
                        else:
                            t_unfreeze = 0
                        saved = _save_frame_if_new(shot, round(elapsed, 2), log_snapshot)
                        self._log(f"  [capture-node] freeze={t_freeze*1000:.0f}ms shot={t_shot*1000:.0f}ms unfreeze={t_unfreeze*1000:.0f}ms saved={saved}")
                    except Exception as e:
                        self._log(f"  [capture-node] exception: {e}")
                    last_executed_count = state["executedCount"]
                    last_screenshot_time = time.time()

                # Periodic capture (with freeze to suppress animation noise)
                elif time.time() - last_screenshot_time >= PERIODIC_INTERVAL:
                    try:
                        t1 = time.time()
                        self._page.evaluate(_QUICK_FREEZE_JS)
                        t_freeze = time.time() - t1
                        t1 = time.time()
                        shot = self._page.screenshot(type="png", animations="disabled", scale="css")
                        t_shot = time.time() - t1
                        t1 = time.time()
                        self._page.evaluate(_QUICK_UNFREEZE_JS)
                        t_unfreeze = time.time() - t1
                        saved = _save_frame_if_new(shot, round(elapsed, 2), log_snapshot)
                        if saved:
                            self._log(f"  [capture-periodic] freeze={t_freeze*1000:.0f}ms shot={t_shot*1000:.0f}ms unfreeze={t_unfreeze*1000:.0f}ms saved=True")
                    except Exception as e:
                        self._log(f"  [capture-periodic] exception: {e}")
                    last_screenshot_time = time.time()

                if state["complete"]:
                    self._log(f"  Execution complete (t={elapsed:.1f}s)")
                    if state["error"]:
                        error_data = state["error"]
                        if isinstance(error_data, dict):
                            error_msg = error_data.get("message", str(error_data))
                            node_error = error_data.get("node_type")
                        else:
                            error_msg = str(error_data)
                            node_error = None
                        self._log(f"  Execution error: {error_msg}")
                        raise WorkflowError(f"Workflow execution failed: {error_msg}", workflow_file=str(workflow_path), node_error=node_error)
                    # Wait for 3D viewers to finish loading before final screenshot
                    has_3d_viewer = self._page.evaluate("""
                        (() => {
                            const nodes = window.app.graph._nodes || [];
                            return nodes.some(n => {
                                const t = (n.type || '').toLowerCase();
                                return t.includes('3d') || t.includes('load3d') || t.includes('preview3d') || t.includes('gaussian');
                            });
                        })()
                    """)
                    if has_3d_viewer:
                        t_3d_start = time.time()
                        # Phase 1: Wait for built-in Load3D widget to finish loading
                        # (Preview3D/Load3D show "Loading 3D Model..." text that disappears when done)
                        try:
                            self._log(f"  [3d-wait] Phase 1: Waiting for 'Loading 3D Model' text to disappear...")
                            t1 = time.time()
                            self._page.wait_for_function("""
                                () => {
                                    const body = document.body.innerText || '';
                                    return !body.includes('Loading 3D Model');
                                }
                            """, timeout=60000)
                            self._log(f"  [3d-wait] Phase 1 done in {time.time()-t1:.1f}s")
                        except Exception as e:
                            self._log(f"  [3d-wait] Phase 1 exception after {time.time()-t1:.1f}s: {e}")

                        # Phase 2: Handle iframe-based viewers (Gaussian splat)
                        has_iframes = self._page.evaluate("document.querySelectorAll('iframe').length > 0")
                        iframe_count = self._page.evaluate("document.querySelectorAll('iframe').length")
                        self._log(f"  [3d-wait] Phase 2: iframes={iframe_count}")
                        if has_iframes:
                            try:
                                matching_logs = [log for log in self._console_logs if "Loaded successfully" in log]
                                viewer_ready = len(matching_logs) > 0
                                self._log(f"  [3d-wait] iframe viewer_ready={viewer_ready} (matched {len(matching_logs)} console logs)")
                                if not viewer_ready:
                                    self._log(f"  [3d-wait] Waiting for 'Loaded successfully' console message (timeout=60s)...")
                                    t1 = time.time()
                                    self._page.wait_for_event(
                                        "console",
                                        predicate=lambda msg: "Loaded successfully" in msg.text,
                                        timeout=60000,
                                    )
                                    self._log(f"  [3d-wait] Got 'Loaded successfully' after {time.time()-t1:.1f}s")
                                # Unfreeze rAF so the viewer can render, then re-freeze.
                                # Use a short timeout to avoid blocking forever in headless
                                # (no vsync means rAF spins the main thread).
                                t1 = time.time()
                                self._page.evaluate(_QUICK_UNFREEZE_JS)
                                self._log(f"  [3d-wait] Unfrozen in {time.time()-t1:.3f}s, waiting for render...")
                                self._page.wait_for_timeout(2000)
                                t1 = time.time()
                                try:
                                    self._page.evaluate(_QUICK_FREEZE_JS, timeout=5000)
                                except Exception:
                                    # If freeze hangs (main thread busy rendering), force-inject via CDP
                                    self._log(f"  [3d-wait] Freeze timed out after {time.time()-t1:.1f}s, force-stopping rAF")
                                    self._page.evaluate("window.requestAnimationFrame = () => 0")
                                self._log(f"  [3d-wait] Frozen after render in {time.time()-t1:.3f}s")
                            except Exception as e:
                                self._log(f"  [3d-wait] iframe wait exception: {e}")
                                try:
                                    self._page.evaluate(_QUICK_FREEZE_JS)
                                except Exception:
                                    pass
                        self._log(f"  [3d-wait] Total 3D wait: {time.time()-t_3d_start:.1f}s")
                    break

                self._page.wait_for_timeout(100)
            else:
                self._log(f"  WARNING: Workflow execution timeout after {timeout}s")

            total_time = round(time.time() - capture_start, 2)
            self._log(f"  Captured {len(frame_paths)} unique frames over {total_time}s")

            # --- Post-execution: final high-quality screenshot ---
            if final_screenshot_path:
                # Diagnostic: check where the preview image chain breaks
                _DIAG_JS = """(() => {
                    const d = {};

                    // Chain steps 2-3: Were CustomEvents dispatched on app.api?
                    d.apiExecutedCount = (window._diagApiExecuted || []).length;
                    d.apiExecutingCount = (window._diagApiExecuting || []).length;

                    // WS tracker vs app stores
                    d.wsOutputNodes = Object.keys(window._executedOutputs || {});
                    d.appNodeOutputKeys = Object.keys(window.app?.nodeOutputs || {});

                    // Check nodePreviewImages specifically (separate from nodeOutputs)
                    const npi = window.app?.nodePreviewImages;
                    d.nodePreviewImagesType = typeof npi;
                    if (npi && typeof npi === 'object') {
                        d.nodePreviewImagesKeys = Object.keys(npi);
                        // Check if it's a Vue reactive proxy
                        d.isProxy = npi.toString?.().includes('Proxy') || false;
                    }

                    // Inspect nodeOutputs structure for one entry
                    const outputs = window.app?.nodeOutputs || {};
                    const firstKey = Object.keys(outputs)[0];
                    if (firstKey) {
                        const val = outputs[firstKey];
                        d.sampleOutputKey = firstKey;
                        d.sampleOutputStructure = JSON.stringify(val).substring(0, 200);
                    }

                    // DOM: ALL elements, not just <img>
                    const allImgs = document.querySelectorAll('img');
                    d.totalImgs = allImgs.length;
                    d.imgSrcs = Array.from(allImgs).slice(0, 5).map(i => i.src.substring(0, 120));

                    // Search for ANY node overlay elements (Vue renders these on top of canvas)
                    d.canvasElements = document.querySelectorAll('canvas').length;
                    // The Vue overlay wraps nodes — check for litegraph node overlay container
                    const graphCanvas = document.querySelector('.graph-canvas-container, .litegraph, [class*="graph"], #graph-canvas');
                    d.graphContainerTag = graphCanvas?.tagName;
                    d.graphContainerClass = graphCanvas?.className?.substring?.(0, 100);

                    // Find the Vue app's root and check for overlay divs
                    const appEl = document.getElementById('app') || document.getElementById('vue-app');
                    d.appElChildren = appEl ? appEl.children.length : 0;

                    // Check ALL divs/elements that are positioned absolutely over the canvas
                    // (Vue preview overlays are typically absolute-positioned)
                    const absElements = document.querySelectorAll('[style*="position: absolute"], [style*="position:absolute"]');
                    d.absoluteElements = absElements.length;

                    // Enumerate properties on window.app to find store references
                    d.appKeys = Object.keys(window.app || {}).filter(k =>
                        k.toLowerCase().includes('preview') ||
                        k.toLowerCase().includes('image') ||
                        k.toLowerCase().includes('output') ||
                        k.toLowerCase().includes('node')
                    );

                    // Graph state
                    const graph = window.app?.graph;
                    d.graphNodeCount = graph?._nodes?.length || 0;
                    d.nodesWithImgs = (graph?._nodes || []).filter(
                        n => n.imgs && n.imgs.length > 0
                    ).length;

                    // Iframe state (3D viewers)
                    const iframes = document.querySelectorAll('iframe');
                    d.iframeCount = iframes.length;
                    d.iframes = Array.from(iframes).map(f => {
                        try {
                            const win = f.contentWindow;
                            return {
                                src: (f.src || '').substring(0, 80),
                                w: f.width || f.clientWidth,
                                h: f.height || f.clientHeight,
                                hasRAF: !!(win && win.requestAnimationFrame),
                                rafFrozen: !!(win && win._origRAF),
                                canvasW: win?.document?.querySelector('canvas')?.width || 0,
                                canvasH: win?.document?.querySelector('canvas')?.height || 0,
                            };
                        } catch(e) { return { src: f.src, error: e.toString() }; }
                    });

                    return d;
                })()"""

                diag = self._page.evaluate(_DIAG_JS)
                # [diag] output silenced — uncomment to debug preview rendering issues:
                # self._log(f"  [diag] API executed: {diag.get('apiExecutedCount')}, executing: {diag.get('apiExecutingCount')}")
                # self._log(f"  [diag] WS outputs: {diag.get('wsOutputNodes')}")
                # self._log(f"  [diag] app.nodeOutputs: {diag.get('appNodeOutputKeys')}")
                # self._log(f"  [diag] nodePreviewImages type={diag.get('nodePreviewImagesType')}, keys={diag.get('nodePreviewImagesKeys', 'N/A')}, isProxy={diag.get('isProxy', 'N/A')}")
                # self._log(f"  [diag] Sample output [{diag.get('sampleOutputKey')}]: {diag.get('sampleOutputStructure', 'N/A')}")
                # self._log(f"  [diag] DOM imgs: {diag.get('totalImgs')}, canvases: {diag.get('canvasElements')}")
                # if diag.get('imgSrcs'):
                #     for src in diag['imgSrcs']:
                #         self._log(f"  [diag]   {src}")
                # self._log(f"  [diag] Abs-positioned elements: {diag.get('absoluteElements')}")
                # self._log(f"  [diag] App #children: {diag.get('appElChildren')}")
                # self._log(f"  [diag] Graph container: <{diag.get('graphContainerTag')}> class={diag.get('graphContainerClass')}")
                # self._log(f"  [diag] App keys (preview/image/output/node): {diag.get('appKeys')}")
                # self._log(f"  [diag] Graph: {diag.get('graphNodeCount')} nodes, {diag.get('nodesWithImgs')} with imgs")

                # Iframe debug info
                iframe_info = diag.get('iframes', [])
                self._log(f"  [debug] iframes: {diag.get('iframeCount', 0)}")
                for i, info in enumerate(iframe_info):
                    if 'error' in info:
                        self._log(f"  [debug]   iframe[{i}]: src={info.get('src', '?')}, error={info['error']}")
                    else:
                        self._log(f"  [debug]   iframe[{i}]: src={info.get('src', '?')}, canvas={info.get('canvasW')}x{info.get('canvasH')}, rafFrozen={info.get('rafFrozen')}")

                t = time.time()
                self._trigger_3d_previews()
                self._log(f"  [timing] trigger_3d_previews: {time.time()-t:.1f}s")

                t = time.time()
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                try:
                    self._screenshot_with_retry(path=str(tmp_path))
                    self._embed_workflow(tmp_path, final_screenshot_path, workflow)
                    self._log(f"  Saved high-quality screenshot: {final_screenshot_path.name}")

                    # Also save as final frame in video folder
                    final_frame_path = output_dir / f"frame_{frame_num:03d}.jpg"
                    img = Image.open(final_screenshot_path)
                    img.save(final_frame_path, "JPEG", quality=webp_quality)
                    img.close()
                    frame_paths.append(final_frame_path)
                    frame_metadata.append({
                        "file": final_frame_path.name,
                        "time": time.time() - capture_start,
                        "log": "Final screenshot",
                    })
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink()

                self._log(f"  [timing] final_screenshot: {time.time()-t:.1f}s")

            # Save metadata.json
            metadata = {
                "frames": frame_metadata,
                "total_time": total_time,
            }
            (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding='utf-8')

            # Fail after screenshots if nodes had validation errors (silently skipped by ComfyUI)
            if node_errors:
                error_summary = "; ".join(
                    f"node {nid}: {errs['errors'][0]['details']}"
                    for nid, errs in node_errors.items()
                    if errs.get("errors")
                )
                raise WorkflowError(
                    f"{len(node_errors)} node(s) had validation errors and were silently skipped: {error_summary}",
                    workflow_file=str(workflow_path),
                )

            return frame_paths

        except Exception:
            raise


def capture_workflows(
    workflow_paths: list[Path],
    output_dir: Optional[Path] = None,
    server_url: str = "http://127.0.0.1:8188",
    width: int = 1920,
    height: int = 1080,
    log_callback: Optional[Callable[[str], None]] = None,
) -> list[Path]:
    """Convenience function to capture multiple workflow screenshots.

    Args:
        workflow_paths: List of workflow JSON file paths
        output_dir: Custom output directory (default: same as each workflow)
        server_url: URL of running ComfyUI server
        width: Viewport width
        height: Viewport height
        log_callback: Optional logging callback

    Returns:
        List of paths to saved screenshots

    Example:
        >>> paths = capture_workflows(
        ...     [Path("workflow1.json"), Path("workflow2.json")],
        ...     server_url="http://localhost:8188",
        ... )
    """
    log = log_callback or (lambda msg: print(msg))
    results = []

    with WorkflowScreenshot(server_url, width, height, log) as ws:
        for workflow_path in workflow_paths:
            if output_dir:
                output_path = output_dir / workflow_path.with_suffix(".png").name
            else:
                output_path = None  # Same directory as workflow

            try:
                result = ws.capture(workflow_path, output_path)
                results.append(result)
            except ScreenshotError as e:
                log(f"  ERROR: {e.message}")
                if e.details:
                    log(f"  {e.details}")

    return results
