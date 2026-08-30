"""Test manager for orchestrating installation tests."""

import faulthandler
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional, Callable, List

from ..common.config import TestConfig, TestLevel, ALL_LEVELS
from ..common.errors import TestError
from .context import LevelContext
from .results import TestResult
from .levels import (
    run_syntax,
    run_coverage,
    run_warnings,
    run_hazards,
    run_install,
    run_registration,
    run_javascript,
    run_instantiation,
    run_static_capture,
    run_validation,
    run_execution_light,
    run_execution,
    run_custom,
)


# Map test levels to their runner functions
LEVEL_RUNNERS = {
    TestLevel.SYNTAX: run_syntax,
    TestLevel.COVERAGE: run_coverage,
    TestLevel.WARNINGS: run_warnings,
    TestLevel.HAZARDS: run_hazards,
    TestLevel.INSTALL: run_install,
    TestLevel.REGISTRATION: run_registration,
    TestLevel.JAVASCRIPT: run_javascript,
    TestLevel.INSTANTIATION: run_instantiation,
    TestLevel.STATIC_CAPTURE: run_static_capture,
    TestLevel.VALIDATION: run_validation,
    TestLevel.EXECUTION_LIGHT: run_execution_light,
    TestLevel.EXECUTION: run_execution,
    TestLevel.CUSTOM: run_custom,
}

# Execution order is the single-sourced ALL_LEVELS (== list(TestLevel)).


class TestManager:
    """Orchestrates installation tests across platforms.

    Args:
        config: Test configuration
        node_dir: Path to custom node directory (default: current directory)
        log_callback: Optional callback for logging
        output_dir: Optional output directory for results

    Example:
        >>> manager = TestManager(config)
        >>> results = manager.run_all()
        >>> for result in results:
        ...     print(f"{result.platform}: {'PASS' if result.success else 'FAIL'}")
    """

    def __init__(
        self,
        config: TestConfig,
        node_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.config = config
        self.node_dir = Path(node_dir) if node_dir else Path.cwd()
        self.output_dir = Path(output_dir) if output_dir else None
        self._original_log = log_callback or (
            lambda msg: print(
                msg.encode('ascii', errors='replace').decode('ascii')
                if isinstance(msg, str) else msg,
                flush=True,
            )
        )
        self._session_log: List[str] = []
        # Structured replay stream: [{"t": float, "s": stream, "m": line}, ...]
        self._events: List[dict] = []
        # [{"t": float, "name": str}, ...] -- jump targets for the replay player
        self._chapters: List[dict] = []
        self._session_start_time: float = 0
        self._session_log_file: Optional[Path] = None
        # Held open across the run; see _append_session_line.
        self._session_log_handle = None
        self._last_session_sync: float = 0.0
        self._level_index = 0
        self._total_levels = 0

    def _get_output_base(self) -> Path:
        """Get the base output directory for logs, screenshots, results."""
        return self.output_dir if self.output_dir else (self.node_dir / "comfy-test-results")

    def _log(self, msg: str, stream: str = "log", echo: bool = True) -> None:
        """Log message with timestamp, write to file immediately.

        Also appends a structured `(t, stream, msg)` event so the run can be
        replayed. `session.log` keeps only the formatted `[MM:SS]` string --
        integer seconds, and a regex away from being usable -- which is why the
        float elapsed is retained here rather than thrown away.
        """
        if self._session_start_time:
            elapsed = time.time() - self._session_start_time
            mins, secs = divmod(int(elapsed), 60)
            timestamp = f"[{mins:02d}:{secs:02d}]"
        else:
            elapsed = 0.0
            timestamp = "[00:00]"

        timestamped_msg = f"{timestamp} {msg}"
        if echo:
            self._original_log(msg)
        self._session_log.append(timestamped_msg)
        self._events.append({"t": round(elapsed, 3), "s": stream, "m": msg})
        if stream == "chapter":
            self._chapters.append({"t": round(elapsed, 3), "name": msg.strip("= ")})

        if self._session_log_file:
            self._append_session_line(timestamped_msg, force_sync=(stream == "chapter"))

    # session.log is the only forensic artifact when a run is OOM-killed or hits
    # the job ceiling, so it has to be on disk -- but this runs on ComfyUI's
    # stdout reader thread (comfyui/server.py routes every line through it).
    # Re-opening and fsyncing per line measured ~2.5 ms against ~0.2 us held
    # open, which backs the pipe up and blocks ComfyUI's own write() -- the
    # logger throttling the process under test. Hold the handle open and sync on
    # a time budget instead: durability within _SYNC_INTERVAL, not per line.
    _SYNC_INTERVAL = 0.5

    def _append_session_line(self, line: str, force_sync: bool = False) -> None:
        try:
            f = self._session_log_handle
            if f is None or f.closed:
                f = open(self._session_log_file, "a", encoding="utf-8")
                self._session_log_handle = f
            f.write(line + "\n")
            f.flush()
            now = time.time()
            if force_sync or now - self._last_session_sync >= self._SYNC_INTERVAL:
                os.fsync(f.fileno())
                self._last_session_sync = now
        except Exception:
            pass

    def _close_session_log(self) -> None:
        """Flush and sync the session log. Safe to call more than once."""
        f = getattr(self, "_session_log_handle", None)
        if f is None or f.closed:
            return
        try:
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass
            self._session_log_handle = None

    def _mark_chapter(self, name: str) -> None:
        """Record a jump target for the replay player."""
        t = (time.time() - self._session_start_time) if self._session_start_time else 0.0
        self._chapters.append({"t": round(t, 3), "name": name})

    def _write_replay(self) -> None:
        """Write install.jsonl: the run as a replayable event stream.

        One JSON object per line, so it greps, diffs and tails like a log while
        still carrying enough timing to replay. Deliberately NOT a video: the
        source is text, and a viewer needs to select and copy the commands.
        """
        if not self._events or not self._session_log_file:
            return
        out = self._session_log_file.parent / "install.jsonl"
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "v": 1,
                    "chapters": self._chapters,
                    "duration": self._events[-1]["t"],
                }) + "\n")
                for ev in self._events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as e:
            self._original_log(f"Could not write {out.name}: {e}")

    def _save_session_log(self) -> None:
        """Finalise the run's log artifacts."""
        # Unconditional sync at teardown: everything buffered since the last
        # budgeted fsync has to reach disk before the process can exit.
        self._close_session_log()
        self._write_replay()
        self._render_install_video()

    def _start_x11_recorder(self, output_base) -> None:
        """Begin an X11 screen recording of the install, if that backend is on.

        Unlike the terminal renderer this cannot run after the fact -- it films
        a live desktop -- so it starts here, before the first level, and is
        stopped in _save_session_log.
        """
        self._x11_recorder = None
        try:
            from ..reporting.install_video import X11Recorder, mode
            if mode() != "x11":
                return
            rec = X11Recorder(self._session_log_file,
                              output_base / "videos" / "install",
                              self._original_log)
            if rec.start():
                self._x11_recorder = rec
        except Exception as e:
            self._original_log(f"Install video (x11) not started: {e}")

    def _render_install_video(self) -> None:
        """Turn install.jsonl into videos/install/, beside the workflow videos.

        After _write_replay, because it reads the file that writes. Never
        fatal: a run that could not film its install is not a failed run.
        """
        if not self._session_log_file:
            return
        rec = getattr(self, "_x11_recorder", None)
        if rec is not None:
            try:
                rec.stop()
            except Exception as e:
                self._original_log(f"Install video (x11) stop failed: {e}")
            self._x11_recorder = None
        try:
            from ..reporting.install_video import render_after_run
            output_base = self._session_log_file.parent
            made = render_after_run(output_base, self._original_log)
            if made or (output_base / "videos" / "install" / "driver.mp4").exists():
                # The report was rendered before install.jsonl existed, so it
                # has no card for this video yet.
                from ..reporting.html_report import generate_html_report
                if (output_base / "results.json").exists():
                    generate_html_report(output_base, self.node_dir.name)
        except Exception as e:
            self._original_log(f"Install video skipped: {e}")
        if self._session_log_file and self._session_log_file.exists():
            self._original_log(f"Session log: {self._session_log_file}")

    def _log_level_start(self, level: TestLevel, in_config: bool) -> None:
        """Log the start of a test level."""
        self._level_index += 1
        level_name = level.value.upper()
        status = "" if in_config else " (implicit)"
        self._log("")
        self._mark_chapter(level_name)
        self._log(f"[{self._level_index}/{self._total_levels}] {level_name}{status}")
        self._log("-" * 40)

    def _log_level_skip(self, level: TestLevel) -> None:
        """Log a skipped level."""
        self._level_index += 1
        level_name = level.value.upper()
        self._log(f"\n[{self._level_index}/{self._total_levels}] {level_name}: SKIPPED")

    def _log_level_done(self, level: TestLevel, message: str = "OK") -> None:
        """Log successful completion of a level."""
        level_name = level.value.upper()
        self._log(f"[{level_name}] {message}")

    def run_all(
        self,
        workflow_filter: Optional[str] = None,
        novram: bool = False,
        vram_debug: bool = False,
    ) -> List[TestResult]:
        """Run tests on all enabled platforms.

        Args:
            workflow_filter: If specified, only run this workflow

        Returns:
            List of TestResult for each platform
        """
        results = []

        platforms = [
            ("linux", self.config.linux),
            ("macos", self.config.macos),
            ("windows", self.config.windows),
            ("windows_portable", self.config.windows_portable),
        ]

        for platform_name, platform_config in platforms:
            if not platform_config.enabled:
                self._log(f"Skipping {platform_name} (disabled)")
                continue

            result = self.run_platform(
                platform_name, workflow_filter,
                novram=novram,
                vram_debug=vram_debug,
            )
            results.append(result)

        return results

    def run_platform(
        self,
        platform_name: str,
        workflow_filter: Optional[str] = None,
        work_dir: Optional[Path] = None,
        novram: bool = False,
        vram_debug: bool = False,
        server_url: Optional[str] = None,
    ) -> TestResult:
        """Run tests on a specific platform.

        Args:
            platform_name: Platform to test
            workflow_filter: If specified, only run this workflow
            work_dir: Use this directory for work

        Returns:
            TestResult for the platform
        """
        # Normalize platform name
        platform_name = platform_name.lower().replace("-", "_")

        # Determine which levels to run.
        # Levels come from comfy-test.toml, full stop. The old `--level` flag
        # let a lane override the config at the command line, which meant the
        # levels that actually ran were a function of the YAML rather than of
        # the pack's own config -- and its truncation silently dropped any
        # level above the flag (`--level execution` cancelled `custom`).
        # Static checks that need no env are reachable via `comfy-test lint`.
        requested_levels = list(self.config.levels)

        # Resolve dependencies
        config_levels = TestLevel.resolve_dependencies(requested_levels)

        # Calculate total levels for progress
        self._level_index = 0
        self._total_levels = len([l for l in ALL_LEVELS if l in config_levels])

        self._log(f"\n{'='*60}")
        self._log(f"Testing: {platform_name}")
        self._log(f"Levels: {', '.join(l.value for l in config_levels)}")
        # Log versions for debugging
        try:
            from importlib.metadata import version as get_version
            self._log(f"comfy-test: {get_version('comfy-test')}")
            try:
                self._log(f"comfy-env: {get_version('comfy-env')}")
            except Exception:
                self._log("comfy-env: not installed")
        except Exception:
            pass
        self._log(f"{'='*60}")

        # Initialize session
        self._session_log = []
        self._events = []
        self._chapters = []
        self._session_start_time = time.time()

        output_base = self._get_output_base()
        output_base.mkdir(parents=True, exist_ok=True)
        self._session_log_file = output_base / "session.log"
        self._session_log_file.write_text("", encoding="utf-8")
        self._close_session_log()          # a re-run must not append to the old handle
        self._last_session_sync = 0.0
        self._start_x11_recorder(output_base)

        # Copy the config that produced this run alongside its output, so it's
        # easy to see what config was used without checking the source repo.
        toml_src = self.node_dir / "comfy-test.toml"
        if toml_src.exists():
            shutil.copy2(toml_src, output_base / "comfy-test.toml")

        # Enable crash dump logging
        crash_log_path = output_base / "crash_dump.log"
        crash_log_file = open(crash_log_path, "w")
        faulthandler.enable(file=crash_log_file)
        self._log(f"Crash dump logging enabled: {crash_log_path}")

        # Create initial context
        ctx = LevelContext(
            config=self.config,
            node_dir=self.node_dir,
            platform_name=platform_name,
            log=self._log,
            output_base=output_base,
            work_dir=work_dir,
            workflow_filter=workflow_filter,
            novram=novram,
            vram_debug=vram_debug,
            server_url=server_url,
        )

        try:
            # Run each level
            for test_level in ALL_LEVELS:
                if test_level not in config_levels:
                    continue

                self._log_level_start(test_level, test_level in requested_levels)

                runner = LEVEL_RUNNERS[test_level]
                ctx = runner(ctx)

                self._log_level_done(test_level, "PASSED")

            self._log(f"\n{platform_name}: PASSED")
            return TestResult(platform_name, True)

        except TestError as e:
            self._log(f"\n{platform_name}: FAILED")
            self._log(f"Error: {e.message}")
            if e.details:
                self._log(f"Details: {e.details}")
            return TestResult(platform_name, False, str(e.message), e.details)

        except Exception as e:
            self._log(f"\n{platform_name}: FAILED (unexpected error)")
            self._log(f"Error: {e}")
            return TestResult(platform_name, False, str(e))

        finally:
            # Cleanup
            if ctx.server:
                try:
                    ctx.server.stop()
                except Exception:
                    pass
            self._save_session_log()
            crash_log_file.close()
