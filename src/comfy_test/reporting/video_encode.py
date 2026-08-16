"""Shared frame-sequence -> mp4 encoding.

Both capture paths produce a `frame_%06d.png` sequence and encode it the same
way:

- the Desktop CDP driver (`platforms/desktop/cdp_driver.py`) polls the Electron
  app at a fixed fps, and
- the server/portable screenshot capture (`reporting/screenshot.py`) in
  `--video` mode polls the headless browser the same way.

Keeping the ffmpeg invocation in one place means the two paths cannot drift on
codec, pixel format, or the odd-dimension padding that libx264 requires.
"""

import re
import subprocess
from pathlib import Path
from typing import Callable, Optional, Union

# libx264 rejects odd width/height; pad up to the next even number.
_EVEN_PAD = "pad=ceil(iw/2)*2:ceil(ih/2)*2"


def resolve_ffmpeg(log: Optional[Callable[[str], None]] = None) -> str:
    """Path to an ffmpeg binary.

    imageio-ffmpeg ships a static binary, so no system ffmpeg install is
    required; fall back to a PATH `ffmpeg` if it is unavailable.
    """
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - environment dependent
        if log:
            log(f"imageio-ffmpeg unavailable ({e}); falling back to PATH ffmpeg")
        return "ffmpeg"


def encode_mp4(
    frames_pattern: Union[str, Path],
    out_mp4: Union[str, Path],
    *,
    fps: int = 5,
    start_number: Optional[int] = None,
    frames_count: Optional[int] = None,
    ffmpeg_exe: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Encode a `frame_%06d.png` sequence to an H.264 mp4. Returns True on success.

    Args:
        frames_pattern: ffmpeg `-i` input pattern, e.g.
            ``frames_dir / "frame_%06d.png"``.
        out_mp4: destination mp4 path.
        fps: input framerate (frames were captured at this cadence).
        start_number: first frame index to read (for slicing one workflow out
            of a longer global sequence). Omit to start from the sequence's
            first frame.
        frames_count: number of frames to read from ``start_number`` (the
            ``-frames:v`` slice length). Omit to read to the end.
        ffmpeg_exe: override the binary; resolved via :func:`resolve_ffmpeg`
            when omitted.
        log: optional sink for the ffmpeg stderr tail on failure.
    """
    ffmpeg_exe = ffmpeg_exe or resolve_ffmpeg(log)

    # `-loglevel error` + `-nostats` mute ffmpeg's ~100 lines of libx264 config
    # and the per-frame progress line; stderr is still captured for the error path.
    cmd = [ffmpeg_exe, "-y", "-loglevel", "error", "-nostats"]
    if start_number is not None:
        cmd += ["-start_number", str(start_number)]
    cmd += ["-framerate", str(fps), "-i", str(frames_pattern)]
    if frames_count is not None:
        cmd += ["-frames:v", str(frames_count)]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", _EVEN_PAD, str(out_mp4)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        # ffmpeg binary not found / not executable. Degrade rather than crash
        # the run -- the video is a diagnostic artifact, not the verdict.
        if log:
            log(f"ffmpeg not runnable ({e}); skipping mp4 encode")
        return False
    if r.returncode != 0:
        if log:
            log(f"ffmpeg failed rc={r.returncode}: {r.stderr[:500]}")
        return False
    return True


def probe_duration(mp4, ffmpeg_exe=None, log=None):
    """Return an mp4's real duration in seconds (via ffmpeg -i), or None.

    Used to clamp the resource-usage graph to exactly what the player shows:
    the timeline encoder's VFR output can come out slightly shorter than the
    sum of frame gaps, so the frame-span estimate is not authoritative.
    """
    ffmpeg_exe = ffmpeg_exe or resolve_ffmpeg(log)
    try:
        r = subprocess.run([ffmpeg_exe, "-i", str(mp4)], capture_output=True, text=True)
    except OSError:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def encode_mp4_timeline(
    frames,
    out_mp4,
    *,
    ffmpeg_exe=None,
    log=None,
    min_duration: float = 0.04,
):
    """Encode PNG frames into an mp4 whose timeline matches real capture timing.

    Unlike :func:`encode_mp4` (fixed input framerate), this honours *when* each
    frame was actually captured, via ffmpeg's concat demuxer with per-frame
    ``duration`` directives. A sparse period (e.g. the frame captured before
    "run", held across validation/queue) renders as that frame *lingering* for
    its real duration instead of the video jumping ahead. Total mp4 duration
    therefore tracks wall-clock execution.

    Args:
        frames: list of ``(png_path, t_elapsed_seconds)`` in capture order. All
            PNGs must live in a single directory (the concat list is written
            there and references them by basename).
        out_mp4: destination mp4 path.
        min_duration: floor on a frame's on-screen time, so two shots captured
            in the same instant still advance the timeline.
    """
    frames = [f for f in frames if f]
    if not frames:
        return False
    ffmpeg_exe = ffmpeg_exe or resolve_ffmpeg(log)

    frames_dir = Path(frames[0][0]).parent
    list_path = frames_dir / "_concat.txt"

    lines = ["ffconcat version 1.0"]
    for i, (path, t) in enumerate(frames):
        # Duration = gap until the next frame; the last frame gets a short tail.
        if i + 1 < len(frames):
            dur = max(min_duration, float(frames[i + 1][1]) - float(t))
        else:
            dur = min_duration
        lines.append(f"file '{Path(path).name}'")
        lines.append(f"duration {dur:.3f}")
    # concat demuxer quirk: the final entry's duration is ignored unless the
    # last file is repeated, so repeat it to honour the tail.
    lines.append(f"file '{Path(frames[-1][0]).name}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error", "-nostats",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-vsync", "vfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", _EVEN_PAD, str(out_mp4),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        if log:
            log(f"ffmpeg not runnable ({e}); skipping mp4 encode")
        return False
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass
    if r.returncode != 0:
        if log:
            log(f"ffmpeg failed rc={r.returncode}: {r.stderr[:500]}")
        return False
    return True
