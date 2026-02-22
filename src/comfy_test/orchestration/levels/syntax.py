"""SYNTAX level - Check project structure, CP1252 compatibility, and forbidden patterns."""

import re
import unicodedata
from pathlib import Path

from ...common.errors import TestError
from ..context import LevelContext


# Patterns that break ComfyUI-native compatibility.
# Each entry: (compiled regex, human-readable description)
FORBIDDEN_PATTERNS = [
    # Device hardcoding — use comfy.model_management.get_torch_device()
    (re.compile(r'\.cuda\s*\('), '.cuda() — use comfy.model_management.get_torch_device()'),
    (re.compile(r'\.to\s*\(\s*["\']cuda'), '.to("cuda...") — use comfy.model_management.get_torch_device()'),
    (re.compile(r'\.to\s*\(\s*torch\.device\s*\(\s*["\']cuda'), '.to(torch.device("cuda")) — use comfy.model_management.get_torch_device()'),
    # Autocast — use operations= (comfy.ops) for dtype management instead
    (re.compile(r'torch\.autocast\s*\('), 'torch.autocast() — use comfy.ops operations= for dtype management'),
    (re.compile(r'torch\.cuda\.amp\.autocast'), 'torch.cuda.amp.autocast — use comfy.ops operations= for dtype management'),
    (re.compile(r'torch\.amp\.autocast'), 'torch.amp.autocast — use comfy.ops operations= for dtype management'),
    # Raw nn layers — use operations.Linear, operations.Conv2d, etc.
    (re.compile(r'nn\.Linear\s*\('), 'nn.Linear() — use operations.Linear() for VRAM management and dtype casting'),
    (re.compile(r'nn\.Conv[123]d\s*\('), 'nn.Conv*d() — use operations.Conv*d() for VRAM management and dtype casting'),
    (re.compile(r'nn\.ConvTranspose[12]d\s*\('), 'nn.ConvTranspose*d() — use operations.ConvTranspose*d()'),
    (re.compile(r'nn\.LayerNorm\s*\('), 'nn.LayerNorm() — use operations.LayerNorm()'),
    (re.compile(r'nn\.GroupNorm\s*\('), 'nn.GroupNorm() — use operations.GroupNorm()'),
    (re.compile(r'nn\.Embedding\s*\('), 'nn.Embedding() — use operations.Embedding()'),
]

# Patterns that print a warning but do not fail the test.
WARNING_PATTERNS = [
    (re.compile(r'torch\.load\s*\('), 'torch.load() ? use comfy.utils.load_torch_file()'),
]


def run(ctx: LevelContext) -> LevelContext:
    """Run SYNTAX level checks.

    Checks:
    1. Project has pyproject.toml or requirements.txt
    2. All Python files use CP1252-safe characters (Windows compatibility)
    3. No forbidden patterns (.cuda(), torch.load, etc.) in model code

    Args:
        ctx: Level context

    Returns:
        Unchanged context (syntax level doesn't add state)

    Raises:
        TestError: If checks fail
    """
    ctx.log(f"[DEBUG] server={ctx.server}, server_url={ctx.server_url}, api={ctx.api}")
    _check_project_structure(ctx)
    _check_unicode_characters(ctx)
    _check_forbidden_patterns(ctx)
    return ctx


def _check_project_structure(ctx: LevelContext) -> None:
    """Check project has dependency file (pyproject.toml or requirements.txt).

    Raises:
        TestError: If neither file exists
    """
    pyproject = ctx.node_dir / "pyproject.toml"
    requirements = ctx.node_dir / "requirements.txt"

    has_pyproject = pyproject.exists()
    has_requirements = requirements.exists()

    if has_pyproject:
        ctx.log("Found pyproject.toml (modern format)")
    if has_requirements:
        ctx.log("Found requirements.txt (legacy format)")

    if not has_pyproject and not has_requirements:
        raise TestError(
            "No dependency file found",
            "Expected pyproject.toml or requirements.txt in node directory"
        )


def _check_unicode_characters(ctx: LevelContext) -> None:
    """Check Python files for characters that can't encode on Windows (cp1252).

    Scans all .py files in the node directory for any characters that
    cannot be encoded in Windows cp1252 codepage. This catches:
    - Curly quotes (copy-pasted from documentation)
    - Emoji and symbols (checkmarks, warning signs, etc.)
    - Non-Latin characters

    Raises:
        TestError: If non-cp1252 characters are found
    """
    issues = []

    # Directories to skip
    skip_dirs = {
        '.git', '__pycache__', '.venv', 'venv', 'node_modules',
        'site-packages', 'lib', 'Lib', '.pixi'
    }

    for py_file in ctx.node_dir.rglob("*.py"):
        # Skip common non-source directories
        rel_path = py_file.relative_to(ctx.node_dir)
        parts = rel_path.parts
        if any(p in skip_dirs or p.startswith('_env_') or p.startswith('.') for p in parts):
            continue

        try:
            content = py_file.read_text(encoding='utf-8')
        except UnicodeDecodeError as e:
            issues.append(f"{rel_path}: Failed to decode as UTF-8: {e}")
            continue

        file_issues = []
        for line_num, line in enumerate(content.splitlines(), 1):
            for col, char in enumerate(line, 1):
                try:
                    char.encode('cp1252')
                except UnicodeEncodeError:
                    char_name = unicodedata.name(char, f'U+{ord(char):04X}')
                    file_issues.append(
                        f"  Line {line_num}, col {col}: {char_name} ({repr(char)}) - not encodable in cp1252"
                    )

        if file_issues:
            issues.append(f"{rel_path}:\n" + "\n".join(file_issues))

    if issues:
        raise TestError(
            "Non-ASCII characters found that can't encode on Windows (cp1252)",
            "Replace with ASCII equivalents:\n\n" + "\n\n".join(issues)
        )

    ctx.log("Unicode check: OK (all characters cp1252-safe)")


def _check_forbidden_patterns(ctx: LevelContext) -> None:
    """Check Python files for patterns that break ComfyUI-native compatibility.

    Catches:
    - .cuda() calls (should use comfy.model_management.get_torch_device())
    - .to("cuda") hardcoded device placement
    - torch.load() (should use comfy.utils.load_torch_file())

    Raises:
        TestError: If forbidden patterns are found
    """
    issues = []
    warnings = []

    skip_dirs = {
        '.git', '__pycache__', '.venv', 'venv', 'node_modules',
        'site-packages', 'lib', 'Lib', '.pixi', 'scripts',
    }

    for py_file in ctx.node_dir.rglob("*.py"):
        rel_path = py_file.relative_to(ctx.node_dir)
        parts = rel_path.parts
        if any(p in skip_dirs or p.startswith('_env_') or p.startswith('.') for p in parts):
            continue

        try:
            content = py_file.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue

        file_issues = []
        file_warnings = []
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.lstrip()
            # Skip comments
            if stripped.startswith('#'):
                continue

            for pattern, description in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    file_issues.append(f"  Line {line_num}: {description}")

            for pattern, description in WARNING_PATTERNS:
                if pattern.search(line):
                    file_warnings.append(f"  Line {line_num}: {description}")

        if file_issues:
            issues.append(f"{rel_path}:\n" + "\n".join(file_issues))
        if file_warnings:
            warnings.append(f"{rel_path}:\n" + "\n".join(file_warnings))

    if warnings:
        ctx.log("Warnings (non-blocking):\n\n" + "\n\n".join(warnings))

    if issues:
        raise TestError(
            "Forbidden patterns found (not ComfyUI-native)",
            "Use ComfyUI APIs instead:\n\n" + "\n\n".join(issues)
        )

    ctx.log("Forbidden patterns check: OK")
