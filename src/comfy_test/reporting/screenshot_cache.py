"""Screenshot caching based on workflow and node file hashes.

This module provides smart caching for workflow screenshots. A screenshot
only needs to be regenerated if:
  - The workflow JSON changed
  - Any node source files used by the workflow changed
  - Any asset files referenced in the workflow changed

Usage:
    cache = ScreenshotCache(node_dir)

    if cache.needs_update(workflow_path, screenshot_path):
        # capture screenshot...
        cache.save_fingerprint(workflow_path, screenshot_path)
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class ScreenshotCache:
    """Manages screenshot cache based on content fingerprints.

    Args:
        node_dir: Path to the custom node directory (contains nodes/ folder)
    """

    def __init__(self, node_dir: Path):
        self.node_dir = Path(node_dir)
        self._node_type_to_file: Optional[Dict[str, Path]] = None

    def needs_update(
        self,
        workflow_path: Path,
        screenshot_path: Path,
    ) -> bool:
        """Check if a screenshot needs to be regenerated.

        Args:
            workflow_path: Path to the workflow JSON file
            screenshot_path: Path where the screenshot would be saved

        Returns:
            True if screenshot should be regenerated, False if cache is valid
        """
        # If screenshot doesn't exist, definitely needs update
        if not screenshot_path.exists():
            return True

        # Load stored fingerprint
        cache_path = self._get_cache_path(screenshot_path)
        if not cache_path.exists():
            return True

        try:
            with open(cache_path, encoding='utf-8-sig') as f:
                stored = json.load(f)
        except (json.JSONDecodeError, OSError):
            return True

        # Compute current fingerprint
        current = self._compute_fingerprint(workflow_path)
        if current is None:
            return True

        # Compare fingerprints
        return stored != current

    def save_fingerprint(
        self,
        workflow_path: Path,
        screenshot_path: Path,
    ) -> None:
        """Save the current fingerprint for a screenshot.

        Call this after successfully capturing a screenshot.

        Args:
            workflow_path: Path to the workflow JSON file
            screenshot_path: Path where the screenshot was saved
        """
        fingerprint = self._compute_fingerprint(workflow_path)
        if fingerprint is None:
            return

        cache_path = self._get_cache_path(screenshot_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(fingerprint, f, indent=2)

    def _get_cache_path(self, screenshot_path: Path) -> Path:
        """Get the cache file path for a screenshot."""
        return screenshot_path.with_suffix('.cache.json')

    def _compute_fingerprint(self, workflow_path: Path) -> Optional[Dict]:
        """Compute a fingerprint for a workflow and its dependencies.

        Returns a dict with hashes of:
          - workflow: hash of the workflow JSON
          - nodes: dict of node_type -> hash of source file
          - assets: dict of asset_path -> hash of file
        """
        if not workflow_path.exists():
            return None

        try:
            with open(workflow_path, encoding='utf-8-sig') as f:
                workflow = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        # Hash the workflow itself
        workflow_hash = self._hash_file(workflow_path)

        # Extract node types and find their source files
        node_types = self._extract_node_types(workflow)
        node_hashes = {}

        for node_type in node_types:
            source_file = self._find_node_source(node_type)
            if source_file and source_file.exists():
                node_hashes[node_type] = self._hash_file(source_file)

        # Extract and hash asset files
        asset_paths = self._extract_asset_paths(workflow)
        asset_hashes = {}

        for asset_path in asset_paths:
            # Try to resolve relative to node_dir
            full_path = self.node_dir / asset_path
            if full_path.exists():
                asset_hashes[asset_path] = self._hash_file(full_path)

        return {
            'workflow': workflow_hash,
            'nodes': node_hashes,
            'assets': asset_hashes,
        }

    def _extract_node_types(self, workflow: dict) -> Set[str]:
        """Extract all node types used in a workflow."""
        node_types = set()

        # Handle standard workflow format with "nodes" array
        if 'nodes' in workflow:
            for node in workflow['nodes']:
                if 'type' in node:
                    node_types.add(node['type'])

        # Handle API format (keys are node IDs)
        else:
            for key, value in workflow.items():
                if isinstance(value, dict) and 'class_type' in value:
                    node_types.add(value['class_type'])

        return node_types

    def _extract_asset_paths(self, workflow: dict) -> Set[str]:
        """Extract asset file paths referenced in a workflow.

        Looks for common patterns like file path widgets, image inputs, etc.
        """
        assets = set()

        def extract_from_value(value):
            """Recursively extract file paths from a value."""
            if isinstance(value, str):
                # Check if it looks like a file path
                if self._looks_like_asset_path(value):
                    assets.add(value)
            elif isinstance(value, list):
                for item in value:
                    extract_from_value(item)
            elif isinstance(value, dict):
                for v in value.values():
                    extract_from_value(v)

        # Handle standard workflow format
        if 'nodes' in workflow:
            for node in workflow['nodes']:
                # Check widgets_values (common place for file paths)
                if 'widgets_values' in node:
                    extract_from_value(node['widgets_values'])
                # Check inputs
                if 'inputs' in node:
                    extract_from_value(node['inputs'])

        # Handle API format
        else:
            for value in workflow.values():
                if isinstance(value, dict) and 'inputs' in value:
                    extract_from_value(value['inputs'])

        return assets

    def _looks_like_asset_path(self, value: str) -> bool:
        """Check if a string looks like an asset file path."""
        # Common asset extensions
        asset_extensions = {
            '.obj', '.stl', '.ply', '.glb', '.gltf', '.fbx', '.blend',  # 3D
            '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff',  # Images
            '.mp4', '.mov', '.avi', '.webm',  # Video
            '.json', '.yaml', '.yml',  # Config
        }

        # Check if it has an asset extension
        lower = value.lower()
        for ext in asset_extensions:
            if lower.endswith(ext):
                return True

        # Check for path-like patterns (contains / or \)
        if ('/' in value or '\\' in value) and '.' in value:
            # But filter out URLs and obvious non-paths
            if not value.startswith(('http://', 'https://', 'data:')):
                return True

        return False

    def _find_node_source(self, node_type: str) -> Optional[Path]:
        """Find the source file for a node type.

        Uses lazy-loaded mapping from node types to their source files.
        """
        if self._node_type_to_file is None:
            self._node_type_to_file = self._build_node_mapping()

        return self._node_type_to_file.get(node_type)

    def _build_node_mapping(self) -> Dict[str, Path]:
        """Build a mapping from node types to their source files.

        Scans all .py files in nodes/ directory and extracts NODE_CLASS_MAPPINGS.
        """
        mapping = {}
        nodes_dir = self.node_dir / 'nodes'

        if not nodes_dir.exists():
            return mapping

        # Find all Python files in nodes/ (excluding __init__.py and _utils/)
        for py_file in nodes_dir.rglob('*.py'):
            # Skip __init__.py files and utility modules
            if py_file.name == '__init__.py':
                continue
            if '_utils' in py_file.parts or py_file.name.startswith('_'):
                continue

            # Try to extract NODE_CLASS_MAPPINGS from the file
            node_types = self._extract_node_types_from_file(py_file)
            for node_type in node_types:
                mapping[node_type] = py_file

        return mapping

    def _extract_node_types_from_file(self, py_file: Path) -> List[str]:
        """Extract node type names from a Python file's NODE_CLASS_MAPPINGS.

        Uses regex to find NODE_CLASS_MAPPINGS dict without executing the code.
        """
        node_types = []

        try:
            content = py_file.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return node_types

        # Pattern to match NODE_CLASS_MAPPINGS = { "NodeName": ... }
        # This handles both single-line and multi-line dicts
        pattern = r'NODE_CLASS_MAPPINGS\s*=\s*\{([^}]+)\}'
        match = re.search(pattern, content, re.DOTALL)

        if match:
            mappings_content = match.group(1)
            # Extract quoted strings that are keys (node names)
            # Pattern: "NodeName" or 'NodeName' followed by :
            key_pattern = r'["\']([^"\']+)["\']\s*:'
            node_types = re.findall(key_pattern, mappings_content)

        return node_types

    def _hash_file(self, file_path: Path) -> str:
        """Compute SHA256 hash of a file."""
        hasher = hashlib.sha256()

        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    hasher.update(chunk)
        except OSError:
            return ''

        return hasher.hexdigest()[:16]  # First 16 chars is enough


def needs_screenshot_update(
    workflow_path: Path,
    screenshot_path: Path,
    node_dir: Path,
) -> bool:
    """Convenience function to check if a screenshot needs updating.

    Args:
        workflow_path: Path to workflow JSON
        screenshot_path: Path to screenshot PNG
        node_dir: Path to custom node directory

    Returns:
        True if screenshot should be regenerated
    """
    cache = ScreenshotCache(node_dir)
    return cache.needs_update(workflow_path, screenshot_path)


def save_screenshot_fingerprint(
    workflow_path: Path,
    screenshot_path: Path,
    node_dir: Path,
) -> None:
    """Convenience function to save screenshot fingerprint.

    Args:
        workflow_path: Path to workflow JSON
        screenshot_path: Path to screenshot PNG
        node_dir: Path to custom node directory
    """
    cache = ScreenshotCache(node_dir)
    cache.save_fingerprint(workflow_path, screenshot_path)
