"""Film the install phase, so `videos/install/` sits beside the workflows.

The execution video is browser screenshots, which can only start once a server
exists -- steps 1 to 10 of a run had no footage at all. This module fills that
gap, with two backends because they answer different questions:

**`terminal`** (default) renders `install.jsonl` -- the timed event stream the
manager already writes -- into frames and encodes them. It needs no display
server, so it is the only backend that reaches every lane: hosted Linux,
Windows and macOS, the CUDA docker containers, and Desktop.

**`x11`** records an actual X session: Xvfb, a real `xterm` tailing the session
log, and `ffmpeg -f x11grab`. That is literally an OS desktop recording, and it
is Linux-only -- the hosted Windows and macOS runners have desktops but not
X11, and the CUDA containers have no desktop at all.

Select with `COMFY_TEST_INSTALL_VIDEO=off|terminal|x11`. Neither backend is
ever fatal: a run that cannot film its install is not a failed run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

_LOG = Callable[[str], None]

#: Frames never exceed this, however long the install took. A 40-minute
#: portable build would otherwise be 24,000 PNGs on disk before encoding.
_MAX_FRAMES = 2400
_DEFAULT_FPS = 6
_COLS, _ROWS = 108, 30

#: Tried in order; the first that loads wins. A proportional fallback still
#: renders readable text, it just does not column-align perfectly.
_MONO_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
)

# Terminal-ish palette. Chapters are the one thing that must be findable when
# scrubbing, so they get the accent colour and a rule.
_BG = (13, 17, 23)
_FG = (201, 209, 217)
_DIM = (110, 118, 129)
_ACCENT = (88, 166, 255)
_ERR = (248, 81, 73)


def mode() -> str:
    """`off`, `terminal` or `x11`. Unknown values fall back to `terminal`."""
    raw = (os.environ.get("COMFY_TEST_INSTALL_VIDEO") or "terminal").strip().lower()
    return raw if raw in ("off", "terminal", "x11") else "terminal"


# --- shared ------------------------------------------------------------------

def _write_metadata(dest_dir: Path, duration: float, speedup: float) -> None:
    """The sidecar html_report discovers to build a gallery card."""
    (dest_dir / "metadata.json").write_text(json.dumps({
        "mp4": "driver.mp4",
        "duration_seconds": round(duration, 1),
        "status": "pass",
        "kind": "install",
        # >1 means wall-clock was compressed to stay under the frame cap.
        "speedup": round(speedup, 2),
    }, indent=2), encoding="utf-8")


# --- backend: terminal -------------------------------------------------------

def _load_events(replay: Path) -> Tuple[List[dict], List[dict], float]:
    """(events, chapters, duration) from install.jsonl."""
    lines = replay.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return [], [], 0.0
    header = json.loads(lines[0])
    events = []
    for line in lines[1:]:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events, header.get("chapters", []), float(header.get("duration") or 0.0)


def _font(size: int):
    from PIL import ImageFont
    for path in _MONO_FONTS:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except Exception:
        return ImageFont.load_default()


def _render_terminal(replay: Path, dest_dir: Path, log: _LOG) -> Optional[Path]:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log("  install video: Pillow unavailable; skipping terminal render")
        return None

    events, chapters, duration = _load_events(replay)
    if not events:
        log("  install video: install.jsonl has no events; nothing to render")
        return None

    fps = _DEFAULT_FPS
    n_frames = max(1, min(_MAX_FRAMES, int(duration * fps) + 1))
    # Seconds of wall-clock per rendered frame. Above the cap this exceeds
    # 1/fps, which is the speed-up; the metadata records it so the report can
    # say so rather than implying real time.
    step = (duration / n_frames) if n_frames else 0.0
    speedup = max(1.0, (step * fps) if step else 1.0)

    size = 15
    font = _font(size)
    cw, lh = size * 0.6, size + 5
    W, H = int(_COLS * cw) + 32, int(_ROWS * lh) + 40

    frames_dir = dest_dir / "_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    buf: List[Tuple[str, tuple]] = []
    ev_i = 0
    for fi in range(n_frames):
        t = fi * step
        while ev_i < len(events) and events[ev_i].get("t", 0) <= t:
            ev = events[ev_i]
            msg, stream = str(ev.get("m", "")), ev.get("s", "")
            colour = (_ACCENT if stream == "chapter"
                      else _ERR if stream == "stderr"
                      else _DIM if stream == "debug"
                      else _FG)
            if stream == "chapter":
                buf.append(("", _DIM))
                msg = f"== {msg.strip('= ')} =="
            # Hard-wrap rather than truncate: a pip line carries the version.
            while len(msg) > _COLS:
                buf.append((msg[:_COLS], colour))
                msg = "  " + msg[_COLS:]
            buf.append((msg, colour))
            ev_i += 1
        del buf[:-_ROWS]

        img = Image.new("RGB", (W, H), _BG)
        d = ImageDraw.Draw(img)
        for row, (text, colour) in enumerate(buf):
            d.text((16, 12 + row * lh), text, font=font, fill=colour)
        mins, secs = divmod(int(t), 60)
        stamp = f"install  {mins:02d}:{secs:02d}"
        if speedup > 1.05:
            stamp += f"  ({speedup:.0f}x)"
        d.text((16, H - 24), stamp, font=font, fill=_DIM)
        img.save(frames_dir / f"frame_{fi + 1:06d}.png")

    from .video_encode import encode_mp4, resolve_ffmpeg
    out = dest_dir / "driver.mp4"
    ok = encode_mp4(frames_dir / "frame_%06d.png", out, fps=fps, log=log,
                    ffmpeg_exe=resolve_ffmpeg(log))
    shutil.rmtree(frames_dir, ignore_errors=True)
    if not ok:
        return None
    _write_metadata(dest_dir, duration, speedup)
    log(f"  install video: videos/install/driver.mp4 "
        f"({n_frames} frames, {duration:.0f}s of install)")
    return out


# --- backend: x11 ------------------------------------------------------------

class X11Recorder:
    """Xvfb + xterm tailing the session log + ffmpeg x11grab.

    A real desktop recording, which is the point -- but it can only show what a
    terminal shows, because nothing in a non-desktop install draws a window.
    Linux only, and silently inert when Xvfb or xterm is missing.
    """

    def __init__(self, session_log: Path, dest_dir: Path, log: _LOG):
        self.session_log, self.dest_dir, self.log = session_log, dest_dir, log
        self.display = f":{99 + (os.getpid() % 20)}"
        self._procs: List[subprocess.Popen] = []
        self._start = 0.0

    def available(self) -> Optional[str]:
        """None if it can run, else the reason it cannot."""
        import sys
        if not sys.platform.startswith("linux"):
            return f"x11 backend is Linux-only (this is {sys.platform})"
        for exe in ("Xvfb", "xterm"):
            if not shutil.which(exe):
                return f"{exe} not on PATH (apt-get install xvfb xterm)"
        return None

    def start(self) -> bool:
        why = self.available()
        if why:
            self.log(f"  install video: {why}; falling back to terminal render")
            return False
        try:
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "DISPLAY": self.display}
            self._spawn(["Xvfb", self.display, "-screen", "0", "1280x720x24"])
            time.sleep(1.0)   # the server must be listening before clients start
            self.session_log.parent.mkdir(parents=True, exist_ok=True)
            self.session_log.touch(exist_ok=True)
            self._spawn(["xterm", "-geometry", "160x40+0+0", "-fa", "Monospace",
                         "-fs", "11", "-bg", "black", "-fg", "grey85",
                         "-e", f"tail -n +1 -f {self.session_log}"], env=env)
            from .video_encode import resolve_ffmpeg
            self._spawn([resolve_ffmpeg(self.log), "-y", "-f", "x11grab",
                         "-framerate", str(_DEFAULT_FPS), "-video_size", "1280x720",
                         "-i", self.display, "-pix_fmt", "yuv420p",
                         "-c:v", "libx264", "-preset", "ultrafast",
                         str(self.dest_dir / "driver.mp4")], env=env)
            self._start = time.time()
            self.log(f"  install video: recording X display {self.display}")
            return True
        except Exception as e:
            self.log(f"  install video: x11 start failed ({e}); falling back")
            self.stop()
            return False

    def _spawn(self, cmd, env=None) -> None:
        self._procs.append(subprocess.Popen(
            cmd, env=env or os.environ, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL))

    def stop(self) -> Optional[Path]:
        """Terminate ffmpeg first so it can finalise the container."""
        duration = time.time() - self._start if self._start else 0.0
        for p in reversed(self._procs):
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._procs.clear()
        out = self.dest_dir / "driver.mp4"
        if out.exists() and out.stat().st_size > 0:
            _write_metadata(self.dest_dir, duration, 1.0)
            self.log(f"  install video: videos/install/driver.mp4 ({duration:.0f}s, x11)")
            return out
        return None


# --- entry point -------------------------------------------------------------

def render_after_run(output_base: Path, log: _LOG) -> Optional[Path]:
    """Build videos/install/ from install.jsonl. No-op unless mode is terminal.

    Called after the run, when install.jsonl is complete. The x11 backend does
    not come through here -- it records live and is finalised by its own stop().
    """
    if mode() != "terminal":
        return None
    replay = output_base / "install.jsonl"
    if not replay.exists():
        return None
    dest = output_base / "videos" / "install"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        return _render_terminal(replay, dest, log)
    except Exception as e:
        log(f"  install video: render failed ({e})")
        return None
