"""The install phase gets filmed too, on every lane.

`driver.mp4` is browser screenshots, so it can only begin once a server exists
-- steps 1 to 10 of a run had no footage at all. Two backends fill that gap and
this pins both:

* `terminal` renders the `install.jsonl` event stream to frames. No display
  server, so it is the only one that reaches hosted Linux/Windows/macOS, the
  CUDA containers and Desktop alike.
* `x11` records a real X session (Xvfb + xterm + ffmpeg x11grab). Linux only,
  and it must degrade quietly everywhere else rather than failing a run.
"""
import json
import sys
from pathlib import Path

import pytest

from comfy_test.reporting import install_video as iv

pillow = pytest.importorskip("PIL", reason="Pillow renders the terminal frames")
pytest.importorskip("imageio_ffmpeg", reason="ships the static ffmpeg binary")


@pytest.mark.parametrize("value,expected", [
    (None, "terminal"), ("terminal", "terminal"), ("x11", "x11"),
    ("off", "off"), ("OFF", "off"), ("  x11 ", "x11"),
    ("nonsense", "terminal"), ("", "terminal"),
])
def test_mode_resolution(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("COMFY_TEST_INSTALL_VIDEO", raising=False)
    else:
        monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", value)
    assert iv.mode() == expected


def _replay(tmp_path: Path, n_chapters: int = 3) -> Path:
    """A small but structurally real install.jsonl."""
    events, t = [], 0.0
    for i in range(n_chapters):
        t += 1.0
        events.append({"t": round(t, 3), "s": "chapter", "m": f"step {i}"})
        for line in range(3):
            t += 0.4
            stream = "stderr" if line == 2 else "stdout"
            events.append({"t": round(t, 3), "s": stream,
                           "m": f"output line {line} for step {i} " + "x" * 140})
    chapters = [{"t": e["t"], "name": e["m"]} for e in events if e["s"] == "chapter"]
    out = tmp_path / "install.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "chapters": chapters,
                            "duration": events[-1]["t"]}) + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")
    return out


def test_terminal_backend_writes_a_playable_video(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "terminal")
    monkeypatch.setattr(iv, "_MAX_FRAMES", 40)
    _replay(tmp_path)
    out = iv.render_after_run(tmp_path, lambda m: None)
    assert out is not None and out.exists()
    # An mp4 with a real moov atom, not a zero-byte stub.
    assert out.stat().st_size > 2000
    assert out == tmp_path / "videos" / "install" / "driver.mp4"


def test_it_lands_beside_the_workflow_videos(tmp_path, monkeypatch):
    """html_report discovers videos/<name>/metadata.json -- install is one."""
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "terminal")
    _replay(tmp_path)
    iv.render_after_run(tmp_path, lambda m: None)
    meta = json.loads((tmp_path / "videos" / "install" / "metadata.json").read_text())
    assert meta["mp4"] == "driver.mp4"          # same shape as a workflow's
    assert meta["kind"] == "install"
    assert meta["status"] == "pass"
    assert meta["duration_seconds"] > 0


def test_frames_are_cleaned_up(tmp_path, monkeypatch):
    """A long install would otherwise leave thousands of PNGs in the artifact."""
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "terminal")
    _replay(tmp_path)
    iv.render_after_run(tmp_path, lambda m: None)
    assert not (tmp_path / "videos" / "install" / "_frames").exists()


def test_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "off")
    _replay(tmp_path)
    assert iv.render_after_run(tmp_path, lambda m: None) is None
    assert not (tmp_path / "videos").exists()


def test_x11_mode_does_not_use_the_after_run_path(tmp_path, monkeypatch):
    """x11 records live; rendering it after the fact would double-write."""
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "x11")
    _replay(tmp_path)
    assert iv.render_after_run(tmp_path, lambda m: None) is None


def test_missing_replay_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "terminal")
    assert iv.render_after_run(tmp_path, lambda m: None) is None


def test_frame_count_is_bounded(tmp_path, monkeypatch):
    """A 40-minute portable build must not stage 24,000 PNGs before encoding."""
    monkeypatch.setenv("COMFY_TEST_INSTALL_VIDEO", "terminal")
    # The cap is what is under test, not its production value; a small one
    # keeps this from rendering thousands of real PNGs in the unit suite.
    monkeypatch.setattr(iv, "_MAX_FRAMES", 40)
    out = tmp_path / "install.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "chapters": [], "duration": 40 * 60}) + "\n")
        for i in range(200):
            f.write(json.dumps({"t": i * 12.0, "s": "stdout", "m": f"line {i}"}) + "\n")
    assert iv.render_after_run(tmp_path, lambda m: None) is not None
    meta = json.loads((tmp_path / "videos" / "install" / "metadata.json").read_text())
    # Compressed rather than truncated: the whole install is still shown.
    assert meta["speedup"] > 1.0
    assert meta["duration_seconds"] == pytest.approx(2400, abs=1)


def test_x11_reports_why_it_cannot_run(tmp_path):
    """It must degrade with a reason, never raise into the run."""
    rec = iv.X11Recorder(tmp_path / "session.log", tmp_path / "vid", lambda m: None)
    why = rec.available()
    if sys.platform.startswith("linux"):
        assert why is None or "not on PATH" in why
    else:
        assert why and "Linux-only" in why


def test_x11_start_is_never_fatal(tmp_path, monkeypatch):
    """Even with a bogus PATH, start() returns False instead of raising."""
    monkeypatch.setenv("PATH", str(tmp_path))
    rec = iv.X11Recorder(tmp_path / "session.log", tmp_path / "vid", lambda m: None)
    assert rec.start() is False
    assert rec.stop() is None
