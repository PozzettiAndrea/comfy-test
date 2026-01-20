"""Workflow screenshot capture using headless browser."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

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

from .errors import TestError

if TYPE_CHECKING:
    from .test.platform.base import TestPaths, TestPlatform
    from .test.config import TestConfig


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

    def __enter__(self) -> "WorkflowScreenshot":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

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
            with open(workflow_path) as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Capturing: {workflow_path.name}")

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

        # Fit the graph to view
        try:
            self._page.evaluate("""
                if (window.app && window.app.canvas) {
                    window.app.canvas.ds.reset();
                    window.app.graph.setDirtyCanvas(true, true);
                }
            """)
            self._page.wait_for_timeout(500)
        except Exception:
            pass  # Best effort

        # Close any open modals (like Templates popup)
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(200)
        except Exception:
            pass

        # Take screenshot with a temp file first
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Full viewport screenshot (1920x1080 at 2x scale)
            self._page.screenshot(path=str(tmp_path))

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
            with open(workflow_path) as f:
                workflow = json.load(f)
        except Exception as e:
            raise ScreenshotError(f"Failed to load workflow: {workflow_path}", str(e))

        self._log(f"Executing and capturing: {workflow_path.name}")

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

        # Queue the workflow for execution and get the prompt_id
        # We call the /prompt API directly rather than using window.app.queuePrompt
        # because the frontend method may not return the prompt_id consistently
        self._log("  Queuing workflow for execution...")
        try:
            result = self._page.evaluate("""
                (async () => {
                    // Get the workflow in API format from the graph
                    const prompt = await window.app.graphToPrompt();

                    // Queue via API directly
                    const resp = await fetch('/prompt', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt: prompt.output })
                    });

                    if (!resp.ok) {
                        const text = await resp.text();
                        return { error: `HTTP ${resp.status}: ${text}` };
                    }

                    return await resp.json();
                })();
            """)
        except Exception as e:
            raise ScreenshotError("Failed to queue workflow for execution", str(e))

        if not result:
            raise ScreenshotError("No response from /prompt API")

        if "error" in result:
            raise ScreenshotError("Failed to queue prompt", result["error"])

        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ScreenshotError("No prompt_id in response", f"Got: {result}")

        self._log(f"  Prompt ID: {prompt_id}")

        # Poll the history API for completion
        self._log("  Waiting for execution to complete...")
        try:
            # Poll history endpoint until status is success or error
            result = self._page.evaluate(f"""
                async () => {{
                    const timeout = {timeout * 1000};
                    const startTime = Date.now();
                    const promptId = "{prompt_id}";

                    while (Date.now() - startTime < timeout) {{
                        try {{
                            const resp = await fetch('/history/' + promptId);
                            const data = await resp.json();
                            const history = data[promptId];

                            if (history) {{
                                const status = history.status?.status_str;
                                if (status === 'success') {{
                                    return {{ success: true, status: 'success' }};
                                }}
                                if (status === 'error') {{
                                    const errorMsg = history.status?.messages?.[0]?.[1] || 'Unknown error';
                                    return {{ success: false, status: 'error', error: errorMsg }};
                                }}
                            }}
                        }} catch (e) {{
                            // Ignore fetch errors, keep polling
                        }}

                        // Wait 1 second before next poll
                        await new Promise(r => setTimeout(r, 1000));
                    }}

                    return {{ success: false, status: 'timeout', error: 'Execution timed out' }};
                }}
            """)
        except Exception as e:
            raise ScreenshotError("Error while waiting for execution", str(e))

        if not result.get("success"):
            status = result.get("status", "unknown")
            error = result.get("error", "Unknown error")
            raise ScreenshotError(f"Workflow execution failed: {status}", error)

        self._log("  Execution completed successfully")

        # Wait for preview nodes to render their outputs
        self._log(f"  Waiting {wait_after_completion_ms}ms for previews to render...")
        self._page.wait_for_timeout(wait_after_completion_ms)

        # Fit the graph to view
        try:
            self._page.evaluate("""
                if (window.app && window.app.canvas) {
                    window.app.canvas.ds.reset();
                    window.app.graph.setDirtyCanvas(true, true);
                }
            """)
            self._page.wait_for_timeout(500)
        except Exception:
            pass  # Best effort

        # Close any open modals (like Templates popup)
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(200)
        except Exception:
            pass

        # Take screenshot with a temp file first
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Full viewport screenshot (1920x1080 at 2x scale)
            self._page.screenshot(path=str(tmp_path))

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
