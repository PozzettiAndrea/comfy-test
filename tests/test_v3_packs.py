"""A V3-only pack must not pass vacuously, nor be failed for existing.

ComfyUI accepts `NODE_CLASS_MAPPINGS` **or** a `comfy_entrypoint` returning
classes whose schema carries the node id (`ComfyUI/nodes.py:2292-2331`).
comfy-test read only the first form, which broke in opposite directions:

* `instantiation` found an empty dict, instantiated nothing, and logged
  "All 0 node(s) instantiated successfully!" -- a green level that ran no code
* `coverage` found no registrations and failed the pack with "Found 0
  registered nodes", accusing a pack that registers perfectly well
"""
import ast
import json
import tempfile
from pathlib import Path

from comfy_test.comfyui.coverage import discover_registered_nodes
from comfy_test.orchestration.levels.instantiation import INSTANTIATION_SCRIPT

_V3_SOURCE = '''
from comfy_api.latest import io

class MyLoader(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MyLoader", display_name="My Loader")

class MySampler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="MySampler")

async def comfy_entrypoint():
    ...
'''


def _pack(**files) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        (d / f"{name}.py").write_text(body, encoding="utf-8")
    return d


def test_v3_schema_node_ids_count_as_registrations():
    names, warnings = discover_registered_nodes(_pack(v3_nodes=_V3_SOURCE))
    assert names == {"MyLoader", "MySampler"}, names
    assert not warnings


def test_v1_and_v3_can_coexist():
    names, _ = discover_registered_nodes(_pack(
        v3_nodes=_V3_SOURCE,
        v1_nodes='NODE_CLASS_MAPPINGS = {"OldStyle": object}\n'))
    assert names == {"MyLoader", "MySampler", "OldStyle"}


def test_a_pack_with_no_nodes_is_still_empty():
    """The guard must not start inventing nodes."""
    names, _ = discover_registered_nodes(_pack(util='def helper():\n    return 1\n'))
    assert names == set()


def _render_script() -> str:
    import re
    keys = sorted(set(re.findall(r"(?<!\{)\{([a-z_]+)\}(?!\})", INSTANTIATION_SCRIPT)))
    return INSTANTIATION_SCRIPT.format(
        **{k: (repr("x") if ("dir" in k or "name" in k) else
               (json.dumps([]) if k.endswith("_json") else "False"))
           for k in keys})


def test_instantiation_script_is_valid_python():
    ast.parse(_render_script())


def test_instantiation_script_handles_both_api_versions():
    src = _render_script()
    for probe in ("comfy_entrypoint", "get_node_list", "GET_SCHEMA",
                  "NODE_CLASS_MAPPINGS", "api_version"):
        assert probe in src, probe


def test_instantiation_refuses_to_pass_on_zero_nodes():
    """"All 0 node(s) instantiated successfully" was the vacuous pass."""
    level = Path(
        __file__).resolve().parent.parent / "src/comfy_test/orchestration/levels/instantiation.py"
    body = level.read_text(encoding="utf-8")
    assert "exposed 0 instantiable nodes" in body
