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
