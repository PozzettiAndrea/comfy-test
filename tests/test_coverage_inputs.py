"""Declared input-value coverage ([test.coverage.inputs]).

A pack can declare that specific values of a node's input must all be
exercised by workflows -- per-option coverage for combo widgets that
multiplex several code paths inside one node, without splitting the node
into per-backend dispatch targets.
"""

import json
import tempfile
from pathlib import Path

import pytest

from comfy_test.common.config_file import load_config
from comfy_test.common.errors import ConfigError
from comfy_test.comfyui.coverage import analyze_coverage


NODE_SRC = '''
NODE_CLASS_MAPPINGS = {"MyLoader": object, "MySink": object}
'''


def _litegraph_node(node_id: int, node_type: str, model: str) -> dict:
    """A litegraph node whose single widget is named 'model'."""
    return {
        "id": node_id,
        "type": node_type,
        "inputs": [
            {"name": "image", "type": "IMAGE", "link": None},
            {"name": "model", "type": "COMBO", "widget": {"name": "model"}},
        ],
        "widgets_values": [model],
    }


def _make_pack(workflow_models: list, api_models: list = ()) -> Path:
    """Synthetic pack: one registered loader + workflows selecting models."""
    pack = Path(tempfile.mkdtemp())
    (pack / "nodes.py").write_text(NODE_SRC)
    wf_dir = pack / "workflows"
    wf_dir.mkdir()
    nodes = [_litegraph_node(i, "MyLoader", m) for i, m in enumerate(workflow_models)]
    nodes.append({"id": 99, "type": "MySink", "inputs": [], "widgets_values": []})
    (wf_dir / "main.json").write_text(json.dumps({"nodes": nodes}))
    if api_models:
        api = {
            str(i): {"class_type": "MyLoader", "inputs": {"model": m}}
            for i, m in enumerate(api_models)
        }
        (wf_dir / "api.json").write_text(json.dumps(api))
    return pack


DECLARED = {"MyLoader": {"model": ["small.safetensors", "large.safetensors"]}}


def test_missing_value_detected_with_provenance():
    pack = _make_pack(["small.safetensors"])
    result = analyze_coverage(pack, input_coverage=DECLARED)
    assert result.untested_input_values == [
        ("MyLoader", "model", ["large.safetensors"])
    ]
    assert result.input_hits["MyLoader"]["model"]["small.safetensors"] == ["main.json"]


def test_full_coverage_passes():
    pack = _make_pack(["small.safetensors", "large.safetensors"])
    result = analyze_coverage(pack, input_coverage=DECLARED)
    assert result.untested_input_values == []


def test_api_format_credits_values():
    pack = _make_pack(["small.safetensors"], api_models=["large.safetensors"])
    result = analyze_coverage(pack, input_coverage=DECLARED)
    assert result.untested_input_values == []
    assert result.input_hits["MyLoader"]["model"]["large.safetensors"] == ["api.json"]


def test_unknown_node_type_warns_not_crashes():
    pack = _make_pack(["small.safetensors"])
    result = analyze_coverage(
        pack, input_coverage={"NoSuchNode": {"model": ["x"]}}
    )
    assert any("unknown node type" in w and "NoSuchNode" in w for w in result.warnings)


def test_never_resolving_input_warns():
    # Node appears in workflows, but the declared input name doesn't exist.
    pack = _make_pack(["small.safetensors"])
    result = analyze_coverage(
        pack, input_coverage={"MyLoader": {"renamed_input": ["x"]}}
    )
    assert any("renamed_input" in w and "never" in w for w in result.warnings)


def test_undeclared_values_do_not_satisfy_requirements():
    pack = _make_pack(["other.safetensors"])
    result = analyze_coverage(pack, input_coverage=DECLARED)
    assert result.untested_input_values == [
        ("MyLoader", "model", ["small.safetensors", "large.safetensors"])
    ]
    # ...but the observed value is still recorded for provenance/warnings.
    assert "other.safetensors" in result.input_hits["MyLoader"]["model"]


def test_no_declaration_changes_nothing():
    pack = _make_pack(["small.safetensors"])
    result = analyze_coverage(pack)
    assert result.input_declared == {}
    assert result.input_hits == {}
    assert result.untested_input_values == []


# ----------------------------------------------------------------------
# comfy-test.toml parsing
# ----------------------------------------------------------------------

def _cfg(body: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "comfy-test.toml").write_text(
        body + '\n[test.platforms]\nplatforms = ["linux-cpu"]\n')
    return d / "comfy-test.toml"


def test_toml_dotted_keys_parse_to_inputs():
    cfg = load_config(_cfg(
        "[test]\n"
        "[test.coverage.inputs]\n"
        'MyLoader.model = ["small.safetensors", "large.safetensors"]\n'
    ))
    assert cfg.coverage.inputs == DECLARED


def test_toml_missing_coverage_section_defaults_empty():
    cfg = load_config(_cfg("[test]"))
    assert cfg.coverage.inputs == {}


def test_toml_invalid_values_rejected():
    with pytest.raises(ConfigError):
        load_config(_cfg(
            "[test]\n"
            "[test.coverage.inputs]\n"
            "MyLoader.model = []\n"  # empty list: nothing to require
        ))
    with pytest.raises(ConfigError):
        load_config(_cfg(
            "[test]\n"
            "[test.coverage]\n"
            'typo_key = ["x"]\n'  # unknown key under [test.coverage]
        ))
