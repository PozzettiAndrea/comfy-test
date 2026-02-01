"""Model directory tracker for logging downloaded models."""

import json
import os
from pathlib import Path
from typing import Any


# Directories to skip entirely (part of ComfyUI clone, not downloaded)
_SKIP_DIRS = {"configs"}

# File patterns to exclude
_PLACEHOLDER_PREFIX = "put_"


def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _should_skip_file(rel_path: str) -> bool:
    """Check if a file should be excluded from the report."""
    name = os.path.basename(rel_path)

    # Placeholder files
    if name.startswith(_PLACEHOLDER_PREFIX):
        return True

    # Lock files and .locks directories
    if name.endswith(".lock"):
        return True
    if "/.locks/" in f"/{rel_path}" or rel_path.startswith(".locks/"):
        return True

    # HuggingFace refs metadata (tiny pointer files)
    if "/refs/" in f"/{rel_path}" or rel_path.startswith("refs/"):
        return True

    return False


def _infer_source(folder_files: list[str]) -> dict[str, Any]:
    """Infer download source from file paths within a folder.

    Looks for HuggingFace cache patterns (models--ORG--REPO) in any file path.

    Args:
        folder_files: List of relative paths (relative to the folder root)

    Returns:
        Dict with 'type' and optional 'repo'/'url' keys.
    """
    for rel_path in folder_files:
        parts = Path(rel_path).parts
        for part in parts:
            if part.startswith("models--"):
                segments = part.split("--", 2)
                if len(segments) == 3:
                    org, repo = segments[1], segments[2]
                    return {
                        "type": "huggingface",
                        "repo": f"{org}/{repo}",
                        "url": f"https://huggingface.co/{org}/{repo}",
                    }
    return {"type": "unknown"}


def build_models_report(models_dir: Path) -> dict[str, Any]:
    """Build a report of the models directory structure.

    Scans the models directory, groups files by top-level subdirectory,
    filters out placeholders and metadata, and infers download sources.

    Args:
        models_dir: Path to ComfyUI's models/ directory

    Returns:
        Report dict with summary and per-folder file listings.
        Returns empty folders dict if models_dir doesn't exist.
    """
    if not models_dir.exists():
        return {
            "models_dir": str(models_dir),
            "summary": {"total_files": 0, "total_size_bytes": 0, "total_size_human": "0 B"},
            "folders": {},
        }

    # Collect all files grouped by top-level subdirectory
    folder_data: dict[str, list[dict]] = {}

    for root, _dirs, files in os.walk(models_dir, followlinks=False):
        for fname in files:
            full_path = Path(root) / fname

            # Skip symlinks (HuggingFace snapshots are symlinks to blobs)
            if full_path.is_symlink():
                continue

            rel_to_models = full_path.relative_to(models_dir)
            rel_str = str(rel_to_models)

            # Must be inside a subdirectory
            parts = rel_to_models.parts
            if len(parts) < 2:
                continue

            top_dir = parts[0]

            # Skip excluded directories
            if top_dir in _SKIP_DIRS:
                continue

            # Skip filtered files
            if _should_skip_file(rel_str):
                continue

            try:
                size = full_path.stat().st_size
            except OSError:
                continue

            # Path relative to the top-level folder
            rel_within_folder = str(Path(*parts[1:]))

            if top_dir not in folder_data:
                folder_data[top_dir] = []

            folder_data[top_dir].append({
                "path": rel_within_folder,
                "size_bytes": size,
                "size_human": _human_size(size),
            })

    # Build output structure
    folders_output: dict[str, Any] = {}
    total_files = 0
    total_size = 0

    for folder_name in sorted(folder_data.keys()):
        file_list = folder_data[folder_name]
        if not file_list:
            continue

        # Sort by size descending
        file_list.sort(key=lambda f: f["size_bytes"], reverse=True)

        folder_size = sum(f["size_bytes"] for f in file_list)
        source = _infer_source([f["path"] for f in file_list])

        folders_output[folder_name] = {
            "total_size_bytes": folder_size,
            "total_size_human": _human_size(folder_size),
            "source": source,
            "files": file_list,
        }

        total_files += len(file_list)
        total_size += folder_size

    return {
        "models_dir": str(models_dir),
        "summary": {
            "total_files": total_files,
            "total_size_bytes": total_size,
            "total_size_human": _human_size(total_size),
        },
        "folders": folders_output,
    }


def save_models_report(report: dict[str, Any], output_dir: Path) -> Path:
    """Save model report to output directory.

    Args:
        report: Report dict from build_models_report
        output_dir: Directory to write models.json

    Returns:
        Path to the written file.
    """
    output_file = output_dir / "models.json"
    output_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return output_file
