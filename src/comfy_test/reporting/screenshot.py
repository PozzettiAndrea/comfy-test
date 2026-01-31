"""Workflow screenshot capture using headless browser."""

import hashlib
import json
import subprocess
import sys
import tempfile
import time
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

    python = str(python_path) if python_path else sys.executable

    try:
        # Install playwright and pillow
        result = subprocess.run(
            [python, "-m", "pip", "install", "playwright", "pillow"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
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

        # If we installed to a different Python environment, we can't verify
        # via import in the current process - just trust the subprocess succeeded
        if python_path:
            return True

        # Update availability flags and import globals (only for current env)
        # We need to set the global names so WorkflowScreenshot can use them
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
        self._browser = self._playwright.chromium.launch(headless=True)
        self._page = self._browser.new_page(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=2,  # HiDPI for crisp screenshots
        )
        # Increase default timeout for CI environments (macOS can be slow)
        self._page.set_default_timeout(60000)
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
            output_path.write_text("\n".join(self._console_logs))

    def clear_console_logs(self) -> None:
        """Clear captured console logs."""
        self._console_logs.clear()

    def _screenshot_with_retry(self, path: str, retries: int = 3, **kwargs) -> None:
        """Take screenshot with retry logic for flaky CI environments.

        Args:
            path: Path to save screenshot
            retries: Number of retry attempts (default 3)
            **kwargs: Additional arguments passed to page.screenshot()
        """
        last_error = None
        for attempt in range(retries):
            try:
                self._page.screenshot(path=path, **kwargs)
                return
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    self._log(f"  Screenshot attempt {attempt + 1} failed, retrying...")
                    self._page.wait_for_timeout(1000)  # Wait before retry
        raise last_error

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

    def _disable_first_run_tutorial(self) -> None:
        """Set server-side setting to prevent Templates panel from showing."""
        try:
            # Call ComfyUI's /settings API to mark tutorial as completed
            requests.post(
                f"{self.server_url}/settings/Comfy.TutorialCompleted",
                json=True,
                timeout=5,
            )
        except Exception:
            pass  # Best effort - server might not be running yet

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

    def _validate_workflow_in_browser(self) -> None:
        """Validate workflow using browser's graphToPrompt() conversion.

        Must be called after workflow is loaded into browser via loadGraphData().
        Uses graphToPrompt() for consistent conversion - this ensures we validate
        using the exact same API format that queuePrompt() will use.

        The browser's graphToPrompt() is the canonical conversion. Validating with
        its output prevents mismatches between Python's converter and the browser's.

        Raises:
            ScreenshotError: If workflow validation fails
        """
        result = self._page.evaluate("""
            async () => {
                try {
                    // Get API format using browser's converter
                    const { output } = await window.app.graphToPrompt();

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
            raise ScreenshotError("Workflow validation failed", details)

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

        # Set server-side setting to prevent Templates panel from showing
        self._disable_first_run_tutorial()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=30000,
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

        # Set server-side setting to prevent Templates panel from showing
        self._disable_first_run_tutorial()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=30000,
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
        timeout: int = 300,
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

        # Set server-side setting to prevent Templates panel from showing
        self._disable_first_run_tutorial()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=30000,
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
        timeout: int = 300,
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

        # Set server-side setting to prevent Templates panel from showing
        self._disable_first_run_tutorial()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=30000,
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
            self._page.screenshot(path=str(initial_frame))
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
                    self._page.screenshot(path=str(frame_path))
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
            self._page.screenshot(path=str(final_frame))
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
        timeout: int = 300,
    ) -> List[Path]:
        """Capture workflow execution as individual WebP frames for slider playback.

        Takes periodic screenshots during execution and saves as compressed WebP.
        Uses 1x scale (not HiDPI) to reduce file size. Also saves metadata.json
        with timestamps and log snapshots for each frame.

        Optionally captures a high-quality PNG screenshot after execution completes,
        waiting for previews to fully render.

        Args:
            workflow_path: Path to the workflow JSON file
            output_dir: Directory to save frames (e.g., videos/workflow_name/)
            webp_quality: WebP compression quality 0-100 (default: 60, lower for video)
            log_lines: Optional list that accumulates log lines (for syncing logs to frames)
            final_screenshot_path: Optional path to save high-quality PNG after execution
            final_screenshot_delay_ms: Delay before final screenshot (default: 5000ms)

        Returns:
            List of paths to saved WebP frames

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

        # Set server-side setting to prevent Templates panel from showing
        self._disable_first_run_tutorial()

        # Navigate to ComfyUI
        try:
            self._page.goto(self.server_url, wait_until="networkidle")
        except Exception as e:
            raise ScreenshotError(f"Failed to connect to ComfyUI at {self.server_url}", str(e))

        # Wait for app to initialize
        try:
            self._page.wait_for_function(
                "typeof window.app !== 'undefined' && window.app.graph !== undefined",
                timeout=30000,
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

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_paths: List[Path] = []
        temp_frames: List[tuple] = []  # (path, timestamp, log_snapshot)

        try:
            # Take initial frame (before execution)
            frame_num = 0
            capture_start = time.time()
            temp_path = output_dir / f"frame_{frame_num:03d}.png"
            self._page.screenshot(path=str(temp_path), scale="css")  # 1x scale
            log_snapshot = "\n".join(log_lines) if log_lines else ""
            temp_frames.append((temp_path, 0.0, log_snapshot))

            # Validate workflow using browser's graphToPrompt() conversion
            self._log("  Validating workflow...")
            self._validate_workflow_in_browser()

            # Queue using queuePrompt for proper WebSocket handling
            self._log("  Queuing workflow for execution...")
            self._page.evaluate("window.app.queuePrompt(0)")

            # Capture loop - periodic screenshots with timeout
            last_screenshot_time = 0
            frame_num = 1
            screenshot_interval_ms = 100  # Capture every 100ms
            timed_out = False

            while time.time() - capture_start < timeout:
                current_time = time.time()
                elapsed = current_time - capture_start

                # Check execution state
                state = self._page.evaluate("""
                    () => ({
                        complete: window._executionComplete,
                        error: window._executionError
                    })
                """)

                # Take periodic screenshot
                if (current_time - capture_start - last_screenshot_time) * 1000 >= screenshot_interval_ms:
                    temp_path = output_dir / f"frame_{frame_num:03d}.png"
                    self._page.screenshot(path=str(temp_path), scale="css")  # 1x scale
                    log_snapshot = "\n".join(log_lines) if log_lines else ""
                    temp_frames.append((temp_path, round(elapsed, 2), log_snapshot))
                    frame_num += 1
                    last_screenshot_time = elapsed

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

                self._page.wait_for_timeout(20)
            else:
                # Timeout reached
                timed_out = True
                self._log(f"  WARNING: Workflow execution timeout after {timeout}s")

            # Final frame after completion
            self._page.wait_for_timeout(1000)
            elapsed = time.time() - capture_start
            temp_path = output_dir / f"frame_{frame_num:03d}.png"
            self._page.screenshot(path=str(temp_path), scale="css")
            log_snapshot = "\n".join(log_lines) if log_lines else ""
            temp_frames.append((temp_path, round(elapsed, 2), log_snapshot))

            total_time = round(time.time() - capture_start, 2)
            self._log(f"  Captured {len(temp_frames)} frames over {total_time}s")

            # Dedupe and convert to JPEG, keeping metadata
            last_hash = None
            frame_num = 0
            frame_metadata = []
            for temp_path, timestamp, log_snap in temp_frames:
                img = Image.open(temp_path)
                h = _image_hash(img)

                if h != last_hash:
                    # Save as JPEG
                    jpg_path = output_dir / f"frame_{frame_num:03d}.jpg"
                    img.save(jpg_path, "JPEG", quality=webp_quality)
                    frame_paths.append(jpg_path)
                    frame_metadata.append({
                        "file": jpg_path.name,
                        "time": timestamp,
                        "log": log_snap,
                    })
                    frame_num += 1
                    last_hash = h

                img.close()
                # Remove temp PNG
                temp_path.unlink()

            self._log(f"  {len(frame_paths)} unique frames saved as WebP")

            # Capture high-quality final screenshot if requested
            if final_screenshot_path:
                self._log(f"  Waiting {final_screenshot_delay_ms}ms for previews to render...")
                self._page.wait_for_timeout(final_screenshot_delay_ms)

                # Fit graph to view again (in case previews changed layout)
                self._fit_graph_to_view()
                self._page.wait_for_timeout(500)

                # Take high-quality screenshot at 2x scale
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                try:
                    # Full viewport screenshot at HiDPI (2x scale for crisp image)
                    self._screenshot_with_retry(path=str(tmp_path))

                    # Embed workflow metadata into PNG
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

            # Save metadata.json (after final screenshot so it's included)
            metadata = {
                "frames": frame_metadata,
                "total_time": total_time,
            }
            (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding='utf-8')

            return frame_paths

        except Exception as e:
            # Clean up on error
            for temp_path, _, _ in temp_frames:
                if isinstance(temp_path, Path) and temp_path.exists():
                    temp_path.unlink()
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
