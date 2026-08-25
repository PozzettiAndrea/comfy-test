"""Contract tests for the JavaScript collision lint (reporting/js_lint.py).

Pure-function: feed JS source strings, assert the rule + severity. No browser,
no server, no install.
"""

import pytest

from comfy_test.reporting.js_lint import lint_source, lint_web_dir


def _rules(findings):
    return sorted((f.level, f.rule) for f in findings)


def test_clean_glue_has_no_findings():
    src = """
    app.registerExtension("geompack.preview", {
        async nodeCreated(node) {
            const f = document.createElement('iframe');
            window.addEventListener('message', (e) => {
                if (e.source !== f.contentWindow) return;
                handle(e.data);
            });
        }
    });
    class MyNode { }
    MyNode.prototype.onExecuted = function () { paint(); };
    """
    assert lint_source(src, "clean.js", ["geompack"]) == []


def test_global_write_is_error_and_ignores_comments_and_strings():
    src = (
        'window.THREE = threeExports;\n'
        'globalThis.vtk = v;\n'
        'window.__THREE__ = 1;\n'          # allowlisted (three.js own guard)
        '// window.COMMENT = 1\n'          # comment -> not flagged
        'const s = "window.STRING = 2";\n'  # string -> not flagged
    )
    findings = lint_source(src, "b.js", ["geompack"])
    assert _rules(findings) == [("error", "global-write"), ("error", "global-write")]
    assert {f.line for f in findings} == {1, 2}


def test_namespaced_global_write_is_also_an_error():
    # Isolation standard: NO globals at all -- namespacing a global makes it
    # collision-safe against other packs, but it is still shared-realm state
    # with no sanctioned platform API behind it.
    assert _rules(lint_source("window.geompack_state = {};", "x.js", ["geompack"])) == [
        ("error", "global-write")]


def test_unnamespaced_extension_is_always_an_error():
    # There is no "guessed prefix" tier any more: the namespace comes from the
    # pack's published identity, so a foreign prefix is an error, not a hint.
    src = 'app.registerExtension("unirig.fbxpreview", {});'
    assert _rules(lint_source(src, "x.js", ["geompack"])) == [
        ("error", "unnamespaced-extension")]
    # a matching namespace clears it entirely
    assert lint_source(src, "x.js", ["unirig"]) == []


def test_extension_name_from_object_form():
    src = 'app.registerExtension({ name: "other.thing", setup() {} });'
    assert _rules(lint_source(src, "x.js", ["geompack"])) == [
        ("error", "unnamespaced-extension")]


def test_custom_element_namespacing():
    assert _rules(lint_source('customElements.define("x-panel", C);', "x.js", ["gp"])) == [
        ("error", "unnamespaced-custom-element")]
    assert lint_source('customElements.define("gp-panel", C);', "x.js", ["gp"]) == []


def test_message_listener_guarded_vs_unguarded():
    guarded = ("window.addEventListener('message', (e) => {"
               " if (e.source !== fr.contentWindow) return; go(e.data); });")
    guarded_origin = ("window.addEventListener('message', (e) => {"
                      " if (e.origin !== location.origin) return; go(e.data); });")
    unguarded = "window.addEventListener('message', (e) => { go(e.data); });"
    assert lint_source(guarded, "x.js", []) == []
    assert lint_source(guarded_origin, "x.js", []) == []
    # inline handler whose whole body lacks source/origin -> ERROR (a fact)
    assert _rules(lint_source(unguarded, "x.js", [])) == [
        ("error", "unguarded-message-listener")]


def test_delegated_message_handler_is_warn_not_error():
    # handler passed by reference -- its body (and any source check) is hidden,
    # so we can only warn, never hard-error.
    src = "window.addEventListener('message', createErrorHandler(panel));"
    assert _rules(lint_source(src, "x.js", [])) == [
        ("warn", "unguarded-message-listener")]


def test_postmessage_type_receiver_split():
    # Pairwise send to YOUR OWN iframe's contentWindow: cannot collide with any
    # other pack's listener -> no finding, prefixed or not.
    pairwise = 'iframe.contentWindow.postMessage({ type: "LOAD_MESH", x: 1 }, "*");'
    assert lint_source(pairwise, "x.js", ["geometrypack"]) == []
    # Shared receivers (parent / window / unknown): unprefixed type -> error.
    shared = 'window.parent.postMessage({ type: "SCREENSHOT" }, "*");'
    assert _rules(lint_source(shared, "x.js", ["geometrypack"])) == [
        ("error", "unprefixed-message-type")]
    # prefixed with the namespace + colon -> clean
    ok = 'window.parent.postMessage({ type: "geometrypack:SCREENSHOT" }, "*");'
    assert lint_source(ok, "x.js", ["geometrypack"]) == []
    # a message object built from a variable is not a literal -> not flagged
    assert lint_source('window.parent.postMessage(msg, "*");',
                       "x.js", ["geometrypack"]) == []


def test_shared_object_monkeypatch_errors_but_own_class_does_not():
    # Isolation standard: patching a shared global -> ERROR (nothing
    # guarantees two packs' patches compose)
    assert _rules(lint_source("LiteGraph.prototype.onDrawForeground = f;", "x.js", [])) == [
        ("error", "shared-object-monkeypatch")]
    assert _rules(lint_source("app.registerNodeType = f;", "x.js", [])) == [
        ("error", "shared-object-monkeypatch")]
    assert _rules(lint_source("api.apiURL = f;", "x.js", [])) == [
        ("error", "shared-object-monkeypatch")]
    # overriding the node's OWN class prototype -> the normal pattern, not flagged
    assert lint_source("nodeType.prototype.onExecuted = f;", "x.js", []) == []


def test_document_level_listener_is_error():
    assert _rules(lint_source(
        "document.addEventListener('paste', onPaste);", "x.js", [])) == [
        ("error", "document-level-listener")]
    assert _rules(lint_source(
        "document.body.addEventListener('keydown', k);", "x.js", [])) == [
        ("error", "document-level-listener")]
    # listening on your OWN element is fine
    assert lint_source("input.addEventListener('keydown', k);", "x.js", []) == []


def test_shared_dom_injection_is_error():
    assert _rules(lint_source(
        "document.body.appendChild(menu);", "x.js", [])) == [
        ("error", "shared-dom-injection")]
    assert _rules(lint_source(
        "document.head.append(style);", "x.js", [])) == [
        ("error", "shared-dom-injection")]
    # injecting into core's chrome DOM via a query chain
    assert _rules(lint_source(
        "document.querySelector('.comfy-menu').appendChild(panel);", "x.js", [])) == [
        ("error", "shared-dom-injection")]
    # appending inside your OWN widget DOM is the sanctioned pattern
    assert lint_source("wrap.appendChild(menu);", "x.js", []) == []
    assert lint_source("node.widgetContainer.append(el);", "x.js", []) == []


def test_bare_import_specifier_warns():
    assert _rules(lint_source("import * as THREE from 'three';", "x.js", [])) == [
        ("warn", "unresolvable-bare-import")]
    # relative and absolute imports resolve fine
    assert lint_source("import { app } from '../../scripts/app.js';", "x.js", []) == []
    assert lint_source("import x from '/extensions/pack/js/x.js';", "x.js", []) == []


# --- Tier-1 hardening: catch honest packs via the aliases they actually use ---

def test_global_object_aliases_when_unbound():
    # self/top/parent/frames ARE the global object in the main realm.
    assert _rules(lint_source("self.THREE = x;", "x.js", [])) == [
        ("error", "global-write")]
    assert _rules(lint_source("parent.foo = x;", "x.js", [])) == [
        ("error", "global-write")]


def test_global_object_alias_shadowed_by_local_is_not_flagged():
    # `const self = this` (and a DOM `parent`) are ordinary locals -> no FP.
    assert lint_source("function f(){ const self = this; self.foo = 1; }", "x.js", []) == []
    assert lint_source("const parent = el.parentNode; parent.x = 1;", "x.js", []) == []


def test_object_assign_and_defineproperty_on_shared_targets():
    assert _rules(lint_source("Object.assign(window, {THREE: x});", "x.js", [])) == [
        ("error", "global-write")]
    assert _rules(lint_source('Object.defineProperty(window, "THREE", d);', "x.js", [])) == [
        ("error", "global-write")]
    assert _rules(lint_source("Object.assign(LGraphCanvas.prototype, {draw: f});", "x.js", [])) == [
        ("error", "shared-object-monkeypatch")]
    # assigning onto your own object is fine
    assert lint_source("Object.assign(myState, {a: 1});", "x.js", []) == []


def test_prototype_pollution_and_widget_registry_are_errors():
    for src in ("Object.prototype.pwned = 1;", "Array.prototype.z = 2;",
                "ComfyWidgets.MYTYPE = function(){};"):
        assert _rules(lint_source(src, "x.js", [])) == [
            ("error", "shared-object-monkeypatch")], src


def test_shared_document_write_cookie_and_stylesheets():
    assert _rules(lint_source('document.cookie = "s=1";', "x.js", [])) == [
        ("error", "shared-document-write")]
    assert _rules(lint_source("document.adoptedStyleSheets = [s];", "x.js", [])) == [
        ("error", "shared-document-write")]


def test_unprefixed_storage_key():
    assert _rules(lint_source('localStorage.setItem("theme", v);', "x.js", ["gp"])) == [
        ("error", "unprefixed-storage-key")]
    assert lint_source('localStorage.setItem("gp:theme", v);', "x.js", ["gp"]) == []
    # guessed namespace -> advisory
    assert _rules(lint_source('sessionStorage.getItem("theme");', "x.js", ["gp"])) == [
        ("error", "unprefixed-storage-key")]


def test_unprefixed_broadcast_channel():
    assert _rules(lint_source('new BroadcastChannel("comfy");', "x.js", ["gp"])) == [
        ("error", "unprefixed-broadcast-channel")]
    assert lint_source('new BroadcastChannel("gp:bus");', "x.js", ["gp"]) == []


def test_mjs_pulled_in_by_a_js_import_is_scanned(tmp_path):
    sub = tmp_path / "js"; sub.mkdir()
    (sub / "glue.js").write_text('import "./leak.mjs"; app.registerExtension({name:"gp.x"});',
                                 encoding="utf-8")
    (sub / "leak.mjs").write_text("window.THREE = x;", encoding="utf-8")
    (sub / "orphan.mjs").write_text("window.VTK = y;", encoding="utf-8")  # imported by nobody
    findings = lint_web_dir(tmp_path, ["gp"])
    files = {f.file for f in findings}
    assert "js/leak.mjs" in files       # pulled into the main realm via import -> scanned
    assert "js/orphan.mjs" not in files  # never imported -> stays iframe-exempt


def test_mjs_files_are_not_scanned(tmp_path):
    # A bundle that writes window.THREE, but as .mjs -> ComfyUI never auto-imports
    # it into the main realm, so it is exempt.
    (tmp_path / "bundle.mjs").write_text("window.THREE = x;", encoding="utf-8")
    (tmp_path / "glue.js").write_text('app.registerExtension("gp.x", {});', encoding="utf-8")
    findings = lint_web_dir(tmp_path, ["gp"])
    assert findings == []


def test_web_dir_scan_reports_relative_paths(tmp_path):
    sub = tmp_path / "js"
    sub.mkdir()
    (sub / "leak.js").write_text("window.THREE = x;", encoding="utf-8")
    findings = lint_web_dir(tmp_path, ["gp"])
    assert len(findings) == 1
    assert findings[0].file == "js/leak.js"
    assert findings[0].level == "error"


# --- DisplayName-derived namespace (the pack's required prefix) ---
from comfy_test.orchestration.levels.javascript import _display_namespace


def _pyproject(tmp_path, body):
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_display_namespace_from_display_name(tmp_path):
    p = _pyproject(tmp_path, '[tool.comfy]\nDisplayName = "GeometryPack"\n')
    assert _display_namespace(p) == "geometrypack"


def test_display_namespace_strips_spaces_and_punctuation(tmp_path):
    p = _pyproject(tmp_path, '[tool.comfy]\nDisplayName = "My 3D Nodes!"\n')
    assert _display_namespace(p) == "my3dnodes"


def test_display_namespace_falls_back_to_project_name_minus_comfyui(tmp_path):
    p = _pyproject(tmp_path, '[project]\nname = "comfyui-geometrypack"\n')
    assert _display_namespace(p) == "geometrypack"


def test_display_namespace_none_without_identity(tmp_path):
    assert _display_namespace(tmp_path) is None  # no pyproject at all


# --- injected CSS: page-global selectors ---

def test_global_css_selectors_are_flagged():
    # a bare type selector and the universal restyle the whole page
    assert _rules(lint_source(
        "const s = `<style>select { background:red }</style>`;", "x.js", ["gp"])) == [
        ("error", "global-css")]
    assert _rules(lint_source(
        'el.innerHTML = "<style>* { margin:0 }</style>";', "x.js", ["gp"])) == [
        ("error", "global-css")]
    assert _rules(lint_source(
        'sheet.insertRule("button { color:red }", 0);', "x.js", ["gp"])) == [
        ("error", "global-css")]


def test_css_scoped_to_pack_namespace_is_allowed():
    assert lint_source(
        "const s = `<style>.gp-panel select { color:#ccc }</style>`;", "x.js", ["gp"]) == []
    assert lint_source('sheet.insertRule(".gp-x { color:red }", 0);', "x.js", ["gp"]) == []


def test_inline_styles_are_not_css_injection():
    # setting element.style is scoped to that element -> never flagged
    assert lint_source('el.style.cssText = "color:red"; el.style.margin = "0";',
                       "x.js", ["gp"]) == []


def test_css_injection_with_dynamic_content_warns():
    # a <style> whose CSS isn't a static literal -> can't verify -> advisory warn
    assert _rules(lint_source(
        'const s = document.createElement("style"); s.textContent = cssVar;',
        "x.js", ["gp"])) == [("warn", "unscoped-css-injection")]


# --- foreign-node hooking (the squat) ---

NODE_IDS = {"GeomPackPreviewMeshVTK", "GeomPackPreviewMeshVTKBatch"}


def test_foreign_node_hook_by_comparison():
    assert _rules(lint_source(
        'if (nodeData.name === "SomeOtherPackNode") hook();',
        "x.js", ["gp"], NODE_IDS)) == [("error", "foreign-node-hook")]
    assert _rules(lint_source(
        'if (nodeType.comfyClass === "UniRigThing") x();',
        "x.js", ["gp"], NODE_IDS)) == [("error", "foreign-node-hook")]


def test_foreign_node_hook_by_config_property():
    assert _rules(lint_source(
        'const NODES = [{ nodeName: "BVHViewer" }];',
        "x.js", ["gp"], NODE_IDS)) == [("error", "foreign-node-hook")]


def test_own_node_hook_is_allowed():
    assert lint_source('if (nodeData.name === "GeomPackPreviewMeshVTK") hook();',
                       "x.js", ["gp"], NODE_IDS) == []


def test_non_node_dot_name_is_not_flagged():
    # a widget/input .name (object is not nodeData/nodeType) must NOT be flagged
    assert lint_source('if (w.name === "index") doThing();', "x.js", ["gp"], NODE_IDS) == []
    assert lint_source('if (input.name === "folder_path") go();', "x.js", ["gp"], NODE_IDS) == []


def test_foreign_node_rule_skipped_without_node_ids():
    # no node ids known -> can't tell own from foreign -> rule does not fire
    assert lint_source('if (nodeData.name === "Whatever") x();', "x.js", ["gp"], None) == []


# ---------------------------------------------------------------------------
# lint_web_dir: the plumbing, not just the rules
# ---------------------------------------------------------------------------
# Everything above calls lint_source directly. lint_web_dir is what production
# actually calls (javascript.py:159), and until now nothing exercised it with a
# real node_ids argument.
#
# That gap is exactly how 59 call sites in this file came to pass a stale bool
# into the `node_ids` slot unnoticed: when `namespaces_declared` was deleted,
# 4-arg calls silently rebound to `node_ids=True` instead of failing. Truthy, so
# the foreign-node rule switched ON, and `_v not in node_ids` raises TypeError
# on a bool -- but only for a snippet that reaches that branch, and none did.
# A test that walks a real directory with a real set closes it.

def _web(tmp_path, **files):
    """A web dir on disk. Keys are basenames with `_` for `.` (kwargs cannot
    contain a dot), so `ext_js` becomes `ext.js` -- and it must, since
    lint_web_dir globs `**/*.js` and would otherwise scan an empty directory
    and pass vacuously."""
    d = tmp_path / "web"
    d.mkdir()
    for name, text in files.items():
        (d / name.replace("_", ".")).write_text(text, encoding="utf-8")
    assert list(d.rglob("*.js")), "fixture wrote no .js -- test would be vacuous"
    return d


def test_lint_web_dir_forwards_node_ids_to_the_foreign_node_rule(tmp_path):
    """A real set reaches the rule through the directory walk."""
    web = _web(tmp_path, ext_js='if (nodeData.name === "BVHViewer") hook();')
    assert _rules(lint_web_dir(web, ["gp"], NODE_IDS)) == [("error", "foreign-node-hook")]


def test_lint_web_dir_allows_a_node_the_pack_ships(tmp_path):
    """The other half: own nodes must survive the same path."""
    web = _web(tmp_path,
               ext_js='if (nodeData.name === "GeomPackPreviewMeshVTK") hook();')
    assert [f for f in lint_web_dir(web, ["gp"], NODE_IDS)
            if f.rule == "foreign-node-hook"] == []


def test_lint_web_dir_rejects_a_non_iterable_node_ids(tmp_path):
    """Regression guard for the mis-binding itself.

    Passing a bool where a set belongs must not reach the rule and explode with
    `argument of type 'bool' is not iterable`, an error that reads like a bug in
    js_lint rather than in the caller.
    """
    web = _web(tmp_path, ext_js='if (nodeData.name === "BVHViewer") hook();')
    with pytest.raises((TypeError, AssertionError)):
        lint_web_dir(web, ["gp"], True)
