"""Guard: the hosted platforms wired into test-matrix.yml must match the registry.

test-matrix.yml still enumerates the GitHub-hosted platforms by hand (as `run-*`
setup outputs + one job each). This test fails if that set drifts from
`registry.matrix()['hosted']`, so adding/removing a hosted platform in the
registry forces the workflow to be updated too. (Phase 2 replaces this with
`comfy-test platforms --matrix-json`; until then, this is the seatbelt.)

Skips gracefully if pyyaml isn't installed.
"""

from pathlib import Path

import comfy_test.platforms as P

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test-matrix.yml"


def test_hosted_platforms_match_registry():
    try:
        import yaml
    except ImportError:
        import pytest  # type: ignore
        pytest.skip("pyyaml not installed")
    doc = yaml.safe_load(WF.read_text())
    outputs = doc["jobs"]["setup"]["outputs"]
    wf_hosted = {k[len("run-"):] for k in outputs
                 if k.startswith("run-") and k != "run-publish"}
    reg_hosted = {p.config_key.replace("_", "-") for p in P.PLATFORMS if p.hosted}
    assert wf_hosted == reg_hosted, (
        f"test-matrix.yml hosted platforms {sorted(wf_hosted)} != "
        f"registry hosted {sorted(reg_hosted)}")
    # Every hosted platform must also have a job defined.
    jobs = set(doc["jobs"])
    for ck in reg_hosted:
        job = ck if ck.endswith("-desktop") else f"{ck}-cpu"
        assert job in jobs, f"missing job {job!r} for hosted platform {ck}"


if __name__ == "__main__":
    test_hosted_platforms_match_registry()
    print("ok  test-matrix.yml hosted platforms match registry")
