"""Workflow screenshot capture using headless browser."""

import json
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
        self._page = self._browser.new_page(viewport={"width": self.width, "height": self.height})

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

        # Take screenshot with a temp file first
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Try to screenshot just the canvas, fall back to full page
            canvas = self._page.locator("canvas").first
            if canvas.is_visible():
                canvas.screenshot(path=str(tmp_path))
            else:
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
