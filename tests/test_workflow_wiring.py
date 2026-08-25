"""Every lane must be reachable, and every unreachable workflow must say why.

Two failure modes this guards, in opposite directions:

1. A lane in the registry that no workflow can run -- coverage that looks
   configured and never executes.
2. An "unreferenced" workflow file getting deleted to tidy up. Deleting a
   lane's workflow to silence a lint finding is how a build grid shrinks
   silently, so an uncalled file is allowed -- it just has to carry a note
   explaining its status.
"""
import re
from pathlib import Path

_WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"


#: Called by *consumer* repositories, not from inside this one. A pack's own
#: workflow does `uses: <owner>/comfy-test/.github/workflows/test-matrix.yml@main`,
#: so these have no in-repo caller by design.
_PUBLIC_ENTRYPOINTS = {"test-matrix.yml", "dispatch-test.yml", "pr-gate.yml"}


def _all_workflows():
    return sorted(_WF.glob("*.yml"))


def _referenced_files():
    """Filenames appearing in a `uses: ./.github/workflows/x.yml` clause."""
    refs = set()
    for wf in _all_workflows():
        refs |= set(re.findall(r"uses:\s*\./\.github/workflows/([\w.-]+\.yml)",
                               wf.read_text(encoding="utf-8")))
    return refs


def test_reusable_workflows_are_called_or_explain_themselves():
    referenced = _referenced_files()
    unexplained = []
    for wf in _all_workflows():
        text = wf.read_text(encoding="utf-8")
        # Entry points have their own triggers; they need no caller.
        if not re.search(r"^\s*workflow_call:", text, re.M):
            continue
        if wf.name in referenced or wf.name in _PUBLIC_ENTRYPOINTS:
            continue
        head = "\n".join(text.splitlines()[:25]).upper()
        if "SUPERSEDED" in head or "DEPRECATED" in head or "UNUSED" in head:
            continue
        unexplained.append(wf.name)
    assert not unexplained, (
        "Reusable workflow(s) with no caller and no explanation: "
        f"{unexplained}\n"
        "Either wire it into test-matrix.yml / dispatch-test.yml, or put a "
        "SUPERSEDED note in the first 25 lines saying what replaced it. Do "
        "not delete a lane's workflow to make this pass."
    )


def test_every_hosted_lane_has_a_matrix_job():
    """A hosted lane the matrix cannot run is coverage that never happens."""
    from comfy_test.lanes import LANES
    matrix = (_WF / "test-matrix.yml").read_text(encoding="utf-8")
    dispatch = (_WF / "dispatch-test.yml").read_text(encoding="utf-8")
    missing = [lane.id for lane in LANES
               if lane.hosted and lane.id not in matrix and lane.id not in dispatch]
    assert not missing, f"hosted lanes with no job anywhere: {missing}"


def test_every_lane_is_reachable_from_some_workflow():
    from comfy_test.lanes import LANES
    corpus = "\n".join(wf.read_text(encoding="utf-8") for wf in _all_workflows())
    missing = [lane.id for lane in LANES if lane.id not in corpus]
    assert not missing, (
        f"lanes named in the registry but in no workflow: {missing}\n"
        "Either wire them up or remove them from lanes/registry.py -- a lane "
        "that cannot run is a dashboard column that is always empty."
    )
