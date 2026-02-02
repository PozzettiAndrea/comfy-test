"""SYNTAX level - Check project structure and CP1252 compatibility."""

import unicodedata
from pathlib import Path

from ...common.errors import TestError
from ..context import LevelContext


def run(ctx: LevelContext) -> LevelContext:
    """Run SYNTAX level checks.

    Checks:
    1. Project has pyproject.toml or requirements.txt
    2. All Python files use CP1252-safe characters (Windows compatibility)

    Args:
        ctx: Level context

    Returns:
        Unchanged context (syntax level doesn't add state)

    Raises:
        TestError: If checks fail
    """
    _check_project_structure(ctx)
    _check_unicode_characters(ctx)
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
