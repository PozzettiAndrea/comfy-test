"""WARNINGS level - opt-in antipattern report. Never fails a build.

This level exists for things that are *probably* wrong but not provably so:
layout the maintainers want, habits that cause trouble later, smells that need a
human to judge. Nothing here is conclusive, which is exactly why it is separate
from SYNTAX -- SYNTAX fails a build and so may only contain rules that are right
every time.

Report-only, deliberately. A gate that fails on a judgement call teaches people
to ignore it, and once ignored it stops catching the cases where it was right.
Run it when you want the report:

    # comfy-test.toml
    [test]
    levels = [..., "warnings"]

Adding a check: keep it cheap, static, and honest about false positives. If a
check cannot be written without an allowlist of legitimate exceptions, write the
allowlist -- do not lower the bar by leaving noise in.
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from ..context import LevelContext

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "site-packages", "lib", "Lib", ".pixi", "scripts",
}

# Files that legitimately live at the pack root rather than under nodes/.
# serialization.py is required there by comfy-env (ADR-0015); the rest are
# ComfyUI's and Python's own entry points.
ROOT_ALLOWED = {
    "__init__.py", "install.py", "setup.py", "prestartup_script.py",
    "serialization.py", "conftest.py",
}

# Extensions that are model weights. These belong in ComfyUI's models/ tree,
# not inside custom_nodes/: weights in the pack are invisible to ComfyUI's model
# resolution, cannot be shared between packs, and are destroyed by a reinstall.
WEIGHT_SUFFIXES = {".safetensors", ".ckpt", ".bin", ".pth", ".pt", ".onnx", ".gguf"}

# Absolute paths baked into source. Almost always someone's dev box.
ABS_PATH = re.compile(r'["\'](?:/home/|/Users/|/mnt/|[A-Za-z]:\\\\)[^"\']{6,}["\']')
SYS_PATH = re.compile(r'sys\.path\.(?:append|insert)\s*\(')

# Below this, two identical files are boilerplate rather than a vendored copy.
DUPLICATE_MIN_LINES = 100


def _walk(node_dir, suffix=None):
    for path in node_dir.rglob("*"):
        rel = path.relative_to(node_dir)
        if any(p in SKIP_DIRS or p.startswith((".", "_env_")) for p in rel.parts):
            continue
        if not path.is_file():
            continue
        if suffix and path.suffix != suffix:
            continue
        yield path, rel


def _check_layout(node_dir):
    """Node source belongs under nodes/."""
    stray = [str(rel) for path, rel in _walk(node_dir, ".py")
             if len(rel.parts) == 1 and rel.name not in ROOT_ALLOWED]
    if not stray:
        return []
    return [f"{len(stray)} .py file(s) at the pack root rather than under nodes/: "
            + ", ".join(sorted(stray)[:8])]


def _check_weights_in_pack(node_dir):
    """Weights belong in ComfyUI's models/ tree."""
    found = [(rel, path.stat().st_size)
             for path, rel in _walk(node_dir)
             if path.suffix in WEIGHT_SUFFIXES]
    if not found:
        return []
    total = sum(size for _, size in found) / 1e6
    biggest = sorted(found, key=lambda x: -x[1])[:5]
    return [f"{len(found)} weight file(s) inside the pack ({total:.0f} MB). "
            f"These belong under models/ so ComfyUI can find and share them:\n"
            + "\n".join(f"    {s/1e6:8.1f} MB  {r}" for r, s in biggest)]


def _check_absolute_paths(node_dir):
    """A path from the author's machine will not exist on anyone else's."""
    hits = []
    for path, rel in _walk(node_dir, ".py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if ABS_PATH.search(line):
                hits.append(f"    {rel}:{lineno}")
    if not hits:
        return []
    return [f"{len(hits)} hardcoded absolute path(s):\n" + "\n".join(hits[:10])]


def _check_sys_path(node_dir):
    """sys.path edits promote directory names to top-level modules.

    That is how a vendored `utils` or `src` ends up shadowing a real package for
    every other node in the process. Sometimes deliberate -- hence a warning.
    """
    hits = []
    for path, rel in _walk(node_dir, ".py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if SYS_PATH.search(line):
                hits.append(f"    {rel}:{lineno}")
    if not hits:
        return []
    return [f"{len(hits)} sys.path modification(s):\n" + "\n".join(hits[:10])]


def _check_duplicate_files(node_dir):
    """The same upstream file vendored more than once will diverge."""
    by_hash = defaultdict(list)
    for path, rel in _walk(node_dir, ".py"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data.count(b"\n") < DUPLICATE_MIN_LINES:
            continue
        by_hash[hashlib.sha1(data).hexdigest()].append(str(rel))

    dupes = {h: paths for h, paths in by_hash.items() if len(paths) > 1}
    if not dupes:
        return []
    worst = sorted(dupes.values(), key=len, reverse=True)[:5]
    total = sum(len(p) for p in dupes.values())
    return [f"{len(dupes)} file(s) vendored more than once ({total} copies). "
            f"Copies drift; a fix applied to one is silently missing from the rest:\n"
            + "\n".join(f"    x{len(p)}  {p[0]}" for p in worst)]


CHECKS = [
    ("layout", "node source under nodes/", _check_layout),
    ("weights", "weights outside the pack", _check_weights_in_pack),
    ("abs-paths", "no hardcoded absolute paths", _check_absolute_paths),
    ("sys-path", "no sys.path edits", _check_sys_path),
    ("duplicates", "no file vendored twice", _check_duplicate_files),
]


def run(ctx: LevelContext) -> LevelContext:
    """Run every antipattern check and report. Never raises."""
    total = 0
    for name, description, check in CHECKS:
        try:
            findings = check(ctx.node_dir)
        except Exception as exc:  # a warning check must never break a run
            ctx.log(f"[warnings] {name}: check itself failed ({type(exc).__name__}: {exc})")
            continue
        if findings:
            total += len(findings)
            ctx.log(f"\n[warnings] {name} -- {description}")
            for finding in findings:
                ctx.log(f"  {finding}")

    if total == 0:
        ctx.log("Warnings check: clean")
    else:
        ctx.log(f"\nWarnings check: {total} finding(s). "
                f"None of these fail the build -- they need a human to judge.")
    return ctx
