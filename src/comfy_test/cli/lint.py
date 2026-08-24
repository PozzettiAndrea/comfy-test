"""Lint command for comfy-test CLI.

Runs the two STATIC levels -- SYNTAX and JAVASCRIPT -- against a pack directory
without building an environment or booting a ComfyUI server. Both levels are
pure source analysis, so there is nothing for the rest of the ladder to
contribute; ``comfy-test run --level javascript`` only drags in INSTALL and
REGISTRATION because levels run in order, not because the lint needs them.

The levels themselves are reused verbatim (``orchestration.levels.syntax`` and
``.javascript``) rather than reimplemented here, so a standalone run and a full
run can never disagree about what passes.
"""

import json as _json
import sys
import tempfile
from pathlib import Path

from ..common.errors import TestError


def _safe(s) -> str:
    """Sanitize for Windows cp1252 consoles (mirrors run.py)."""
    return str(s).encode("ascii", errors="replace").decode("ascii")


def _load_config(pack_dir: Path):
    """Best-effort TestConfig for the pack.

    Only ``[test.javascript] namespaces`` matters to these two levels, and a
    pack without a comfy-test.toml should still be lintable, so a missing or
    unparseable config degrades to defaults rather than aborting.
    """
    from ..common.config import TestConfig

    toml_path = pack_dir / "comfy-test.toml"
    if toml_path.exists():
        try:
            from ..common.config_file import load_config
            return load_config(toml_path), None
        except Exception as e:
            return TestConfig(name=pack_dir.name), f"comfy-test.toml: {e}"
    return TestConfig(name=pack_dir.name), None


def _make_ctx(pack_dir: Path, config, output_base: Path, sink):
    """A LevelContext with only the fields the static levels read.

    `paths`/`server`/`api` stay None: INSTALL and REGISTRATION never ran, and
    both levels tolerate that.
    """
    from ..orchestration.context import LevelContext

    return LevelContext(
        config=config,
        node_dir=pack_dir,
        platform_name="lint",
        log=sink,
        output_base=output_base,
    )


def _run_one(level_run, ctx):
    """Run one level, returning (passed, TestError | None)."""
    try:
        level_run(ctx)
        return True, None
    except TestError as e:
        return False, e


def cmd_lint(args) -> int:
    """Run the static levels (syntax and/or javascript) on a pack directory."""
    pack_dir = Path(args.path).resolve()
    if not pack_dir.is_dir():
        print(f"[comfy-test] Not a directory: {pack_dir}", file=sys.stderr)
        return 2

    checks = ["syntax", "javascript"] if args.check == "all" else [args.check]
    config, config_warning = _load_config(pack_dir)

    lines: list[str] = []
    results: dict[str, dict] = {}

    # The JAVASCRIPT level drops a javascript.json sidecar next to a run's other
    # artifacts. Point it at a temp dir so a lint never litters the pack, then
    # read it back for --json -- that keeps the structured output identical to
    # what a real run records.
    with tempfile.TemporaryDirectory(prefix="comfy-test-lint-") as tmp:
        tmpdir = Path(tmp)
        ctx = _make_ctx(pack_dir, config, tmpdir, lines.append)

        if "syntax" in checks:
            from ..orchestration.levels.syntax import run as run_syntax
            ok, err = _run_one(run_syntax, ctx)
            results["syntax"] = {
                "passed": ok,
                "error": err.message if err else None,
                "details": err.details if err else None,
            }

        if "javascript" in checks:
            from ..orchestration.levels.javascript import run as run_javascript
            ok, err = _run_one(run_javascript, ctx)
            sidecar = tmpdir / "javascript.json"
            payload = {}
            if sidecar.exists():
                try:
                    payload = _json.loads(sidecar.read_text(encoding="utf-8"))
                except ValueError:
                    payload = {}
            results["javascript"] = {
                "passed": ok,
                "error": err.message if err else None,
                "details": err.details if err else None,
                **payload,
            }

    js = results.get("javascript", {})
    warn_count = js.get("summary", {}).get("warnings", 0)
    failed = [k for k, v in results.items() if not v["passed"]]

    if args.json:
        print(_json.dumps({
            "pack_dir": str(pack_dir),
            "checks": checks,
            "passed": not failed,
            "config_warning": config_warning,
            "results": results,
        }, indent=2))
    else:
        print(_safe(f"Node pack:  {pack_dir}"))
        print(_safe(f"Checks:     {', '.join(checks)}"))
        if config_warning:
            print(_safe(f"  ! {config_warning} (using defaults)"))
        print()
        for line in lines:
            print(_safe(line))
        print()
        for name in checks:
            r = results[name]
            print(_safe(f"[{name.upper()}] {'PASSED' if r['passed'] else 'FAILED'}"))
            if not r["passed"]:
                print(_safe(f"  {r['error']}"))
                if r["details"]:
                    for d in str(r["details"]).splitlines():
                        print(_safe(f"  {d}"))

    if failed:
        return 1
    if args.strict and warn_count:
        if not args.json:
            print(_safe(f"\n--strict: {warn_count} warning(s) treated as failure."))
        return 1
    return 0


def add_lint_parser(subparsers):
    """Add the lint subcommand parser."""
    parser = subparsers.add_parser(
        "lint",
        help="Run the static checks (syntax and/or javascript) with no env or server",
        description=(
            "Run the SYNTAX and/or JAVASCRIPT levels directly against a pack "
            "directory. Pure source analysis: no pixi environment is built and "
            "no ComfyUI server is started, so this works on a bare checkout and "
            "finishes in about a second."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Node pack directory (default: current directory)",
    )
    parser.add_argument(
        "--check", "-k",
        choices=["syntax", "javascript", "all"],
        default="all",
        help="Which static check to run (default: all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also exit non-zero on JavaScript warnings (for CI)",
    )
    parser.set_defaults(func=cmd_lint)
