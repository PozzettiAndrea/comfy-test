"""Guards for three ways a run used to go green while proving nothing.

Each test here corresponds to a failure that CI reported as a pass:

1. a pack that imports cleanly and registers zero nodes (`registration`)
2. `execution` enabled on a pack that ships no workflows at all
3. a workflow that fails on a server we never gave a `client_id` to

The third is the sharpest: without a `client_id` ComfyUI sends *no* terminal
event (`execution_start`/`execution_error`/`execution_success` are all
`broadcast=False`, execution.py:683), so a failing workflow and a passing one
looked identical on the wire. Verified against ComfyUI 0.33.0.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from comfy_test.comfyui.api import _history_failure
from comfy_test.orchestration.levels.registration import attribute_own_nodes


# --- 1. registration: imported fine, registered nothing ----------------------

def _oi(**packs):
    """Build a fake /object_info: {node_name: {"python_module": module}}."""
    out = {"KSampler": {"python_module": "nodes"}}
    for module, names in packs.items():
        for n in names:
            out[n] = {"python_module": module}
    return out


def test_own_nodes_are_attributed_by_directory_name():
    oi = _oi(**{"custom_nodes.ComfyUI-MyPack": ["MyLoader", "MySaver"]})
    own, attributed = attribute_own_nodes(oi, "ComfyUI-MyPack")
    assert attributed is True
    assert sorted(own) == ["MyLoader", "MySaver"]


def test_empty_node_class_mappings_is_zero_own_nodes():
    # The pack imported (no IMPORT FAILED) but contributed nothing.
    oi = _oi(**{"custom_nodes.ComfyUI-Other": ["SomeoneElsesNode"]})
    own, attributed = attribute_own_nodes(oi, "ComfyUI-EmptyPack")
    assert attributed is True
    assert own == ()


def test_a_server_without_attribution_is_unknown_not_zero():
    # A ComfyUI predating server.py:765 reports no python_module anywhere.
    # That must not be read as "registered nothing".
    oi = {"KSampler": {}, "MyLoader": {}}
    own, attributed = attribute_own_nodes(oi, "ComfyUI-MyPack")
    assert attributed is False
    assert own == ()


def test_another_packs_nodes_are_not_credited_to_us():
    oi = _oi(**{"custom_nodes.ComfyUI-Neighbour": ["NeighbourNode"]})
    own, _ = attribute_own_nodes(oi, "ComfyUI-Neighbour-Extra")
    assert own == ()


# --- 3. the client_id false green -------------------------------------------

def test_history_reports_success_as_no_failure():
    assert _history_failure({"status": {"status_str": "success",
                                        "completed": True, "messages": []}}) is None


def test_history_surfaces_the_execution_error_payload():
    payload = {
        "prompt_id": "abc",
        "node_id": "2",
        "node_type": "TrivialBoom",
        "exception_type": "RuntimeError",
        "exception_message": "deliberate failure",
    }
    failure = _history_failure({
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [["execution_start", {"prompt_id": "abc"}],
                         ["execution_error", payload]],
        }
    })
    # Same dict shape the websocket handler assigns, so downstream cannot tell
    # which path detected the failure.
    assert failure == payload


def test_history_failure_without_an_error_message_still_fails():
    failure = _history_failure({"status": {"status_str": "error",
                                           "completed": False, "messages": []}})
    assert failure is not None
    assert failure["exception_type"] == "ExecutionError"


def test_incomplete_run_is_a_failure_even_if_status_str_is_missing():
    failure = _history_failure({"status": {"completed": False, "messages": []}})
    assert failure is not None


def test_missing_status_is_unknown_not_failed():
    # The server recorded no status; that is not evidence of failure.
    assert _history_failure({"outputs": {}}) is None
    assert _history_failure({"status": None}) is None


# --- 2. execution enabled with no workflows ---------------------------------

def _ctx(workflows, skip_workflow=False):
    from types import SimpleNamespace as NS
    return NS(
        platform_name="linux",
        config=NS(
            workflow=NS(workflows=list(workflows)),
            get_platform_config=lambda _n: NS(skip_workflow=skip_workflow),
        ),
    )


def test_execution_with_no_workflows_is_an_error():
    from comfy_test.common.errors import ConfigError
    from comfy_test.orchestration.context import require_workflows
    try:
        require_workflows(_ctx([]), "execution")
    except ConfigError as e:
        msg = str(e)
        # The message must name all three ways out, or it just blocks people.
        assert "workflows/" in msg
        assert "levels" in msg
        assert "skip_workflow" in msg
    else:
        raise AssertionError("expected ConfigError for execution with no workflows")


def test_a_pack_with_workflows_passes():
    from comfy_test.orchestration.context import require_workflows
    require_workflows(_ctx(["basic.json"]), "execution")


def test_skip_workflow_is_the_supported_way_to_run_none():
    from comfy_test.orchestration.context import require_workflows
    require_workflows(_ctx([], skip_workflow=True), "execution")


def test_execution_light_is_gated_too():
    from comfy_test.common.errors import ConfigError
    from comfy_test.orchestration.context import require_workflows
    try:
        require_workflows(_ctx([]), "execution_light")
    except ConfigError as e:
        assert "execution_light" in str(e)
    else:
        raise AssertionError("expected ConfigError for execution_light")


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)


# --- 4. execution and execution_light must not drift apart -------------------
# `execution_light` was a copy-paste fork of `execution`. Three fixes landed in
# one and never crossed to the other: the CPU/CUDA routing guard, checking the
# skip list before registering the log listener, and per-workflow console logs.
# Both now delegate to one body, and these pin that.

def test_both_execution_levels_share_one_body():
    from comfy_test.orchestration.levels import execution, execution_light
    from comfy_test.orchestration.levels import _workflow_run

    assert execution.run_workflows is _workflow_run.run_workflows
    assert execution_light.run_workflows is _workflow_run.run_workflows
    # The forked copies were ~350 and ~275 lines. Wrappers must stay wrappers.
    for mod in (execution, execution_light):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert len(src.splitlines()) < 60, f"{mod.__name__} is growing a body again"


def test_routing_guard_applies_to_both_levels(monkeypatch):
    """The 59-workflow bug: cuda list configured, cpu list empty, CPU runner.

    Falling through here runs every workflow on a runner configured for a
    subset -- reported as a plausible partial result.
    """
    from comfy_test.orchestration.levels._workflow_run import _select_workflows

    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    logged = []
    ctx = _FakeCtx(cpu=[], cuda=["heavy"], log=logged.append)
    assert _select_workflows(ctx, [Path("a.json")], "execution") is None
    assert _select_workflows(ctx, [Path("a.json")], "execution_light") is None
    assert any("running nothing" in m for m in logged)


def test_unconfigured_pack_still_runs_everything(monkeypatch):
    """A pack that never configured routing keeps the old permissive behaviour."""
    from comfy_test.orchestration.levels._workflow_run import _select_workflows

    monkeypatch.setenv("COMFY_TEST_CUDA", "0")
    ctx = _FakeCtx(cpu=[], cuda=[], log=lambda m: None)
    allowed, runner_type = _select_workflows(ctx, [Path("a.json")], "execution")
    assert allowed == set() and runner_type == "CPU"


class _FakeCtx:
    def __init__(self, cpu, cuda, log):
        self.log = log
        self.config = type("C", (), {"workflow": type("W", (), {
            "cpu": cpu, "cuda": cuda})()})()
