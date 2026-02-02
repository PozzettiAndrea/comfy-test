"""Report generation and output utilities."""

from .screenshot_cache import ScreenshotCache

# Note: html_report and screenshot modules are large and imported on-demand
# to avoid slow startup times

__all__ = [
    "ScreenshotCache",
]
