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
    assert lint_source(src, "clean.js", ["geompack"], True) == []


def test_global_write_is_error_and_ignores_comments_and_strings():
    src = (
        'window.THREE = threeExports;\n'
        'globalThis.vtk = v;\n'
        'window.__THREE__ = 1;\n'          # allowlisted (three.js own guard)
        '// window.COMMENT = 1\n'          # comment -> not flagged
        'const s = "window.STRING = 2";\n'  # string -> not flagged
    )
    findings = lint_source(src, "b.js", ["geompack"], True)
    assert _rules(findings) == [("error", "global-write"), ("error", "global-write")]
    assert {f.line for f in findings} == {1, 2}


def test_namespaced_global_write_is_allowed():
    assert lint_source("window.geompack_state = {};", "x.js", ["geompack"], True) == []


def test_unnamespaced_extension_error_when_declared_warn_when_not():
    src = 'app.registerExtension("unirig.fbxpreview", {});'
    assert _rules(lint_source(src, "x.js", ["geompack"], True)) == [
        ("error", "unnamespaced-extension")]
    # not declared -> downgraded to warning (prefix was only guessed)
    assert _rules(lint_source(src, "x.js", ["geompack"], False)) == [
        ("warn", "unnamespaced-extension")]
    # a matching namespace clears it entirely
    assert lint_source(src, "x.js", ["unirig"], True) == []


def test_extension_name_from_object_form():
    src = 'app.registerExtension({ name: "other.thing", setup() {} });'
    assert _rules(lint_source(src, "x.js", ["geompack"], True)) == [
        ("error", "unnamespaced-extension")]


def test_custom_element_namespacing():
    assert _rules(lint_source('customElements.define("x-panel", C);', "x.js", ["gp"], True)) == [
        ("error", "unnamespaced-custom-element")]
    assert lint_source('customElements.define("gp-panel", C);', "x.js", ["gp"], True) == []


def test_message_listener_guarded_vs_unguarded():
    guarded = ("window.addEventListener('message', (e) => {"
               " if (e.source !== fr.contentWindow) return; go(e.data); });")
    unguarded = "window.addEventListener('message', (e) => { go(e.data); });"
    assert lint_source(guarded, "x.js", [], True) == []
    assert _rules(lint_source(unguarded, "x.js", [], True)) == [
        ("warn", "unguarded-message-listener")]


def test_shared_object_monkeypatch_warns_but_own_class_does_not():
    # patching a shared global -> warn
    assert _rules(lint_source("LiteGraph.prototype.onDrawForeground = f;", "x.js", [], True)) == [
        ("warn", "shared-object-monkeypatch")]
    assert _rules(lint_source("app.registerNodeType = f;", "x.js", [], True)) == [
        ("warn", "shared-object-monkeypatch")]
    # overriding the node's OWN class prototype -> the normal pattern, not flagged
    assert lint_source("nodeType.prototype.onExecuted = f;", "x.js", [], True) == []


def test_mjs_files_are_not_scanned(tmp_path):
    # A bundle that writes window.THREE, but as .mjs -> ComfyUI never auto-imports
    # it into the main realm, so it is exempt.
    (tmp_path / "bundle.mjs").write_text("window.THREE = x;", encoding="utf-8")
    (tmp_path / "glue.js").write_text('app.registerExtension("gp.x", {});', encoding="utf-8")
    findings = lint_web_dir(tmp_path, ["gp"], True)
    assert findings == []


def test_web_dir_scan_reports_relative_paths(tmp_path):
    sub = tmp_path / "js"
    sub.mkdir()
    (sub / "leak.js").write_text("window.THREE = x;", encoding="utf-8")
    findings = lint_web_dir(tmp_path, ["gp"], True)
    assert len(findings) == 1
    assert findings[0].file == "js/leak.js"
    assert findings[0].level == "error"
