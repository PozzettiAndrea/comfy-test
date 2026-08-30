"""dispatch-test.yml must route on the lane input, not only the deprecated alias.

`platform` is documented in that file as "DEPRECATED alias for `lane`", but
every job filter and every `runs-on:` read it exclusively. Dispatching with
only `lane` set therefore produced `contains('', 'cuda') == false` -> the CPU
job, and both `startsWith` tests false -> `windows-latest`. The job was still
*named* from `inputs.lane`, so a Windows CPU run published to the dashboard as
a green `linux-cuda`: a pass on hardware that never ran.
"""
import re
from pathlib import Path

_WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_DISPATCH = _WF / "dispatch-test.yml"

# A read of the deprecated alias that is not paired with the lane input.
_BARE = re.compile(r"(?<!inputs\.lane \|\| )inputs\.platform")


def test_no_bare_platform_read_remains():
    offenders = [f"{n}: {line.strip()}"
                 for n, line in enumerate(_DISPATCH.read_text().splitlines(), 1)
                 if _BARE.search(line)]
    assert not offenders, (
        "dispatch-test.yml reads the deprecated `platform` input without "
        "falling back to `lane`:\n  " + "\n  ".join(offenders)
        + "\n\nUse `(inputs.lane || inputs.platform)`."
    )


def _route(value):
    """Mirror of the job filters and runs-on ladder in dispatch-test.yml."""
    is_cuda, is_desktop = "cuda" in value, "desktop" in value
    job = ("desktop_cuda" if value == "windows-desktop-cuda"
           else "desktop" if is_desktop
           else "cuda" if is_cuda
           else "cpu")
    runs = ("ubuntu-latest" if value.startswith("linux")
            else "macos-latest" if value.startswith("macos")
            else "windows-latest")
    return job, runs


def test_lane_alone_routes_to_the_right_job_and_runner():
    """The regression: lane set, platform unset."""
    for lane, job, runs in (
        ("linux-cuda", "cuda", "ubuntu-latest"),
        ("linux-cpu", "cpu", "ubuntu-latest"),
        ("macos-cpu", "cpu", "macos-latest"),
        ("windows-cuda", "cuda", "windows-latest"),
        ("windows-desktop", "desktop", "windows-latest"),
        ("windows-desktop-cuda", "desktop_cuda", "windows-latest"),
    ):
        assert _route(lane) == (job, runs), lane


def test_every_registry_lane_routes_somewhere_sane():
    """No lane may land on a runner whose OS it does not name."""
    from comfy_test.lanes import LANES
    for lane in LANES:
        _job, runs = _route(lane.id)
        expected_os = ("ubuntu-latest" if lane.id.startswith("linux")
                       else "macos-latest" if lane.id.startswith("macos")
                       else "windows-latest")
        assert runs == expected_os, f"{lane.id} -> {runs}"


# --- `comfy-test docker run` shape -------------------------------------------
# The container's entrypoint re-invokes `comfy-test`, so what it installs is
# the harness that actually ran. Both entrypoints honour COMFY_TEST_VERSION and
# fall back to `uv tool install --reinstall comfy-test` -- PyPI latest -- when
# it is unset, and nothing ever set it. A docker run therefore tested a
# different harness than the one invoked, silently.

def _docker_run_src():
    return (Path(__file__).resolve().parent.parent
            / "src/comfy_test/cli/docker/run.py").read_text(encoding="utf-8")


def test_harness_version_is_forwarded_on_both_paths():
    src = _docker_run_src()
    assert src.count("_harness_version_env()") == 3, (
        "expected one definition plus a call on each of the linux and windows "
        "paths; a container that resolves its own comfy-test version is not "
        "running the harness you invoked")


def test_an_explicit_pin_wins(monkeypatch):
    from comfy_test.cli.docker import run as dr
    monkeypatch.setenv("COMFY_TEST_VERSION", "0.3.5")
    assert dr._harness_version_env() == ["-e", "COMFY_TEST_VERSION=0.3.5"]


def test_entrypoints_still_read_the_variable():
    """If an entrypoint stops honouring it, forwarding it is theatre."""
    root = Path(__file__).resolve().parent.parent / "src/comfy_test/_docker"
    for rel in ("linux-cuda/entrypoint.sh", "windows-cuda/entrypoint.ps1"):
        assert "COMFY_TEST_VERSION" in (root / rel).read_text(encoding="utf-8"), rel


def test_desktop_flags_are_not_duplicated_onto_docker_run():
    """`run --desktop` is the spelling; the copies here used no docker at all.

    Reads the built parser rather than the source text, so a comment recording
    why they were removed does not trip the guard against their return.
    """
    import argparse

    from comfy_test.cli.docker.run import add_docker_run_parser

    sub = argparse.ArgumentParser().add_subparsers()
    add_docker_run_parser(sub)
    flags = {opt for p in sub.choices.values() for a in p._actions
             for opt in a.option_strings}
    for flag in ("--desktop_mac", "--desktop_windows", "--desktop_windows_cuda",
                 "--cdp-port", "--monitor-progress"):
        assert flag not in flags, f"{flag} is back on `docker run`"
    # The genuinely docker-specific ones must stay.
    for flag in ("--persist", "--keep-clone", "--no-defender-warn"):
        assert flag in flags, f"{flag} went missing"
