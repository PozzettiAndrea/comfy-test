"""Tests for the shared frame-sequence -> mp4 encoder (reporting/video_encode)."""

import os
import re
import shutil
import subprocess

import pytest

from comfy_test.reporting.video_encode import (
    encode_mp4,
    encode_mp4_timeline,
    resolve_ffmpeg,
)


def _ffmpeg_runnable() -> bool:
    exe = resolve_ffmpeg()
    if exe == "ffmpeg" and shutil.which("ffmpeg") is None:
        return False
    try:
        subprocess.run([exe, "-version"], capture_output=True)
        return True
    except OSError:
        return False


def _write_frames(d, n, size=(321, 241)):
    """Write n odd-dimensioned PNG frames (odd size exercises the pad filter)."""
    Image = pytest.importorskip("PIL.Image")
    for i in range(1, n + 1):
        Image.new("RGB", size, ((i * 20) % 256, 50, 80)).save(
            d / f"frame_{i:06d}.png"
        )


@pytest.mark.skipif(not _ffmpeg_runnable(), reason="no ffmpeg/imageio-ffmpeg available")
def test_encode_mp4_master(tmp_path):
    _write_frames(tmp_path, 11)
    out = tmp_path / "driver.mp4"
    assert encode_mp4(tmp_path / "frame_%06d.png", out, fps=5)
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.skipif(not _ffmpeg_runnable(), reason="no ffmpeg/imageio-ffmpeg available")
def test_encode_mp4_slice(tmp_path):
    """Per-workflow slicing (desktop path) out of a longer global sequence."""
    _write_frames(tmp_path, 11)
    out = tmp_path / "slice.mp4"
    assert encode_mp4(
        tmp_path / "frame_%06d.png", out, fps=5, start_number=3, frames_count=5
    )
    assert out.exists() and out.stat().st_size > 0


def _mp4_duration(path) -> float:
    """Parse an mp4's duration (seconds) from ffmpeg -i stderr."""
    exe = resolve_ffmpeg()
    r = subprocess.run([exe, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    assert m, r.stderr
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


@pytest.mark.skipif(not _ffmpeg_runnable(), reason="no ffmpeg/imageio-ffmpeg available")
def test_encode_mp4_timeline_honours_gaps(tmp_path):
    """A big gap between timestamps must hold the frame, not skip it.

    Frame 1 at t=0 then a 2s gap then 4 frames 0.2s apart -> the pre-run frame
    lingers ~2s and the mp4 duration tracks wall-clock (~2.8s), not 5 frames /
    5fps = 1s.
    """
    Image = pytest.importorskip("PIL.Image")
    ts = [0.0, 2.0, 2.2, 2.4, 2.6]
    frames = []
    for i, t in enumerate(ts, 1):
        p = tmp_path / f"frame_{i:06d}.png"
        Image.new("RGB", (320, 240), ((i * 40) % 256, 60, 90)).save(p)
        frames.append((p, t))
    out = tmp_path / "driver.mp4"
    assert encode_mp4_timeline(frames, out)
    assert out.exists() and out.stat().st_size > 0
    dur = _mp4_duration(out)
    # ~2.0 (hold) + 0.2*3 + tail; assert it reflects the gap, not 1s constant-fps
    assert dur > 2.0, f"timeline collapsed the 2s gap (duration={dur}s)"
    assert not (tmp_path / "_concat.txt").exists(), "concat list not cleaned up"


def test_encode_mp4_missing_ffmpeg_degrades(tmp_path):
    """A missing/unrunnable encoder returns False, never raises (the run goes on)."""
    logs = []
    bogus = os.path.join(str(tmp_path), "definitely-not-ffmpeg")
    ok = encode_mp4(
        tmp_path / "frame_%06d.png",
        tmp_path / "out.mp4",
        ffmpeg_exe=bogus,
        log=logs.append,
    )
    assert ok is False
    assert any("ffmpeg" in m.lower() for m in logs)
