"""`IMPORT FAILED` must accuse the pack under test, not ComfyUI itself.

Upstream logs that string from three places (`ComfyUI/nodes.py:2385`, `:2576`,
`:2587`) and only one is about a custom node:

* `:2576` -- `comfy_api_nodes/` failed, usually a missing optional dependency
* `:2587` -- `comfy_extras/` failed; `nodes_glsl.py` does `ctypes.CDLL(libEGL)`
  at module scope, which fails on any headless runner
* `:2385` -- the import-times summary, printed for every custom node that
  already produced a "Cannot import" line, so counting it double-reports

`registration` turns a non-empty result into a hard failure of the user's pack.
"""
from comfy_test.comfyui.server import scan_import_errors

PACK = "ComfyUI-MyPack"
_CANNOT = (f"Cannot import /root/ComfyUI/custom_nodes/{PACK} module for "
           f"custom nodes: No module named 'trimesh'")
_TIMES = f"   0.0 seconds (IMPORT FAILED): /root/ComfyUI/custom_nodes/{PACK}"
_EXTRAS = "IMPORT FAILED: comfy_extras/nodes_glsl"
_API = "IMPORT FAILED: comfy_api_nodes/nodes_veo"
_OTHER = "Cannot import /root/ComfyUI/custom_nodes/OtherPack module for custom nodes: boom"


def test_our_own_failure_is_reported():
    assert scan_import_errors([_CANNOT], PACK) == [_CANNOT]


def test_upstreams_own_subsystems_are_not_our_fault():
    """A trimmed or headless ComfyUI must not fail somebody else's pack."""
    assert scan_import_errors([_EXTRAS, _API], PACK) == []
    assert scan_import_errors([_EXTRAS, _API]) == []


def test_import_times_line_is_not_counted_twice():
    """One failure, one error -- `N error(s)` was inflated."""
    assert scan_import_errors([_CANNOT, _TIMES], PACK) == [_CANNOT]


def test_another_pack_does_not_fail_ours():
    assert scan_import_errors([_OTHER], PACK) == []
    assert _OTHER in scan_import_errors([_OTHER])


def test_a_clean_boot_reports_nothing():
    assert scan_import_errors(["Starting server", "0.1 seconds: nodes_mask"], PACK) == []


def test_unscoped_still_sees_custom_node_failures():
    """Without a pack name the old permissive behaviour holds, minus upstream."""
    found = scan_import_errors([_CANNOT, _OTHER, _EXTRAS])
    assert _CANNOT in found and _OTHER in found and _EXTRAS not in found
