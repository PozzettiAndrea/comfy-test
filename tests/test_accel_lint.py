"""Contract: the accelerator lazy-import rule, checked statically.

Ported from comfy-env, which had the same four cases but could only guess
import names. The fifth and sixth tests here cover what the move was FOR:
a distribution whose import name differs from its name is resolved exactly
from env.stamp.json, and is reported rather than silently passed when no
stamp is available.
"""

import json

from comfy_test.orchestration.levels.accel import lint_accelerator_imports


def _pack(tmp_path, source, packages='["cumesh"]'):
    nodes = tmp_path / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "comfy-env.toml").write_text(
        f'python = "3.12"\n[cuda]\npackages = {packages}\n', encoding="utf-8")
    (nodes / "bad.py").write_text(source, encoding="utf-8")
    return tmp_path


def _workspace(tmp_path, mapping):
    """A COMFY_ENV_ROOT holding one stamped env."""
    env = tmp_path / "ws" / "envs" / "demo-py313"
    env.mkdir(parents=True, exist_ok=True)
    (env / "env.stamp.json").write_text(
        json.dumps({"abi_tag": "py313", "accel_imports": mapping}), encoding="utf-8")
    return tmp_path / "ws"


def test_unguarded_toplevel_import_is_error(tmp_path):
    root = _pack(tmp_path, "import cumesh\n")
    findings = lint_accelerator_imports(root)
    errors = [f for f in findings if f["level"] == "error"]
    assert len(errors) == 1
    assert "bad.py" in errors[0]["file"]


def test_guarded_toplevel_import_is_warning(tmp_path):
    root = _pack(tmp_path, "try:\n    import cumesh\nexcept ImportError:\n    cumesh = None\n")
    findings = lint_accelerator_imports(root)
    assert [f["level"] for f in findings if "bad.py" in f["file"]] == ["warning"]


def test_lazy_import_in_declared_node_is_clean(tmp_path):
    root = _pack(tmp_path, (
        "class Remesh:\n"
        "    ACCELERATOR = 'cuda'\n"
        "    def execute(self):\n"
        "        import cumesh\n"
        "        return cumesh\n"
    ))
    assert [f for f in lint_accelerator_imports(root) if "bad.py" in f["file"]] == []


def test_torch_cuda_in_undeclared_module_is_warning(tmp_path):
    root = _pack(tmp_path, "import torch\n\nx = torch.cuda.is_available()\n")
    findings = [f for f in lint_accelerator_imports(root) if "bad.py" in f["file"]]
    assert [f["level"] for f in findings] == ["warning"]
    assert "torch.cuda" in findings[0]["message"]


def test_stamp_resolves_import_name_that_differs_from_dist(tmp_path, monkeypatch):
    """faithc-aot installs `faithcontour`; only the stamp knows that."""
    root = _pack(tmp_path, "import faithcontour\n", packages='["faithc-aot"]')
    monkeypatch.setenv(
        "COMFY_ENV_ROOT", str(_workspace(tmp_path, {"faithc-aot": ["faithcontour"]})))
    errors = [f for f in lint_accelerator_imports(root) if f["level"] == "error"]
    assert len(errors) == 1, "stamped mapping must catch the real import name"
    assert "faithcontour" in errors[0]["message"]


def test_unstamped_package_is_reported_not_passed(tmp_path, monkeypatch):
    """No stamp -> say so. Silence here is what the old lint got wrong."""
    root = _pack(tmp_path, "import faithcontour\n", packages='["faithc-aot"]')
    monkeypatch.setenv("COMFY_ENV_ROOT", str(_workspace(tmp_path, {})))

    findings = lint_accelerator_imports(root)
    assert [f for f in findings if f["level"] == "error"] == [], (
        "cannot claim an error it could not verify")
    unresolved = [f for f in findings if "not recorded in any env.stamp.json" in f["message"]]
    assert len(unresolved) == 1, "must report the package it could not check"
    assert "faithc-aot" in unresolved[0]["message"]
