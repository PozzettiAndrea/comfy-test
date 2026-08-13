"""Static collision lint for a pack's ComfyUI frontend JavaScript.

ComfyUI auto-imports every ``*.js`` file under a pack's web dir into the ONE
shared browser page (the main realm). So a pack's glue JS can collide with
every other pack via global writes, duplicate extension names, unguarded
message listeners, and prototype monkeypatches. The discipline that avoids it:
keep heavy rendering in iframes (isolated), keep only thin, namespaced glue in
the main realm.

This module enforces that statically -- the JS twin of comfy-env's accelerator
lint. It parses each ``.js`` file with tree-sitter (real AST, so a
``window.THREE`` inside a comment or string is not a false positive) and reports
findings. ``.mjs`` files are NOT auto-imported by ComfyUI (its glob is
``**/*.js`` only), so they run only inside the iframes that load them -- they
are exempt from the main-realm rules (this is what makes renaming a leaking
bundle to ``.mjs`` a real fix rather than a cosmetic one).

Severity, per the static-analysis-lies doctrine:
  - error: unambiguous, literal violations a parser cannot be wrong about
           (a literal ``window.X =``, a literal ``registerExtension("name")``).
  - warn:  heuristics that dynamic JS can defeat in either direction.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

# Property names on window/globalThis that are the platform's, not a pack's --
# writing these is legitimate and must not be flagged.
_ALLOWED_GLOBAL_PROPS = {"__THREE__"}  # three.js's own multi-instance guard flag

# Shared objects every pack sees. Patching a method on one of these can break
# every other pack. Overriding a method on the node's OWN class prototype
# (nodeType.prototype.onExecuted, the standard extension pattern) is per-node
# and safe, so it is NOT flagged -- only patches rooted at these globals are.
_SHARED_PROTOTYPE_ROOTS = {"LiteGraph", "LGraphCanvas", "LGraphNode", "LGraph", "app"}


def _member_root(node) -> Optional[str]:
    """Leftmost identifier of a member-expression chain (a.b.c -> 'a')."""
    while node is not None and node.type == "member_expression":
        node = node.child_by_field_name("object")
    if node is not None and node.type == "identifier":
        return node.text.decode("utf-8", errors="replace")
    return None


@dataclass
class Finding:
    level: str          # "error" | "warn"
    rule: str           # kebab-case rule id
    file: str           # path relative to the scanned web dir
    line: int           # 1-indexed
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _parser():
    """Build a tree-sitter JavaScript parser (cached at module level)."""
    global _PARSER
    try:
        return _PARSER
    except NameError:
        pass
    import tree_sitter_javascript as tsjs
    from tree_sitter import Language, Parser
    _PARSER = Parser(Language(tsjs.language()))
    return _PARSER


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _string_literal_value(node) -> Optional[str]:
    """Return the inner value of a string/template node, else None."""
    if node is None:
        return None
    if node.type == "string":
        # children: '"' fragment... '"' -- join the fragment text
        inner = "".join(_text(c) for c in node.children if c.type == "string_fragment")
        return inner
    if node.type == "template_string":
        # only treat as literal if it has no ${...} substitutions
        if any(c.type == "template_substitution" for c in node.children):
            return None
        return "".join(_text(c) for c in node.children if c.type not in ("`",))
    return None


def _is_namespaced(name: Optional[str], namespaces: List[str]) -> bool:
    if not name:
        return True  # non-literal name -> can't judge, don't flag
    return any(name == ns or name.startswith(ns + ".") or name.startswith(ns + "-")
               or name.startswith(ns + "_") or name.startswith(ns + ":")
               for ns in namespaces)


def _first_arg_name(call_node) -> Optional[str]:
    """For registerExtension/customElements.define: the first string arg, or
    the object arg's `name:` property value."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.children if c.type not in ("(", ")", ",")), None)
    if first is None:
        return None
    lit = _string_literal_value(first)
    if lit is not None:
        return lit
    if first.type == "object":
        for pair in first.children:
            if pair.type == "pair":
                key = pair.child_by_field_name("key")
                if key is not None and _text(key).strip("\"'") == "name":
                    return _string_literal_value(pair.child_by_field_name("value"))
    return None


def _callee_str(call_node) -> str:
    fn = call_node.child_by_field_name("function")
    return _text(fn) if fn is not None else ""


def lint_source(text: str, rel_path: str, namespaces: List[str],
                namespaces_declared: bool) -> List[Finding]:
    """Lint one .js source string. Returns findings.

    namespaces_declared: when False (pack did not declare its JS namespaces),
    the name-prefix rules are downgraded to warnings -- we cannot hard-error on
    a prefix we only guessed.
    """
    parser = _parser()
    tree = parser.parse(text.encode("utf-8"))
    findings: List[Finding] = []
    name_sev = "error" if namespaces_declared else "warn"

    for node in _walk(tree.root_node):
        line = node.start_point[0] + 1

        # --- global writes: window.X = / globalThis.X = ---
        if node.type == "assignment_expression":
            lhs = node.child_by_field_name("left")
            if lhs is not None and lhs.type == "member_expression":
                obj = lhs.child_by_field_name("object")
                prop = lhs.child_by_field_name("property")
                if obj is not None and _text(obj) in ("window", "globalThis") and prop is not None:
                    pname = _text(prop)
                    if pname not in _ALLOWED_GLOBAL_PROPS and not _is_namespaced(pname, namespaces):
                        findings.append(Finding(
                            "error", "global-write", rel_path, line,
                            f"writes {_text(obj)}.{pname} into the shared main realm "
                            f"(auto-imported .js). Keep it iframe-local, rename the "
                            f"file to .mjs so ComfyUI does not auto-import it, or "
                            f"namespace the global."))

        # --- calls: registerExtension / customElements.define ---
        if node.type == "call_expression":
            callee = _callee_str(node)
            if callee.endswith(".registerExtension") or callee == "registerExtension":
                name = _first_arg_name(node)
                if not _is_namespaced(name, namespaces):
                    findings.append(Finding(
                        name_sev, "unnamespaced-extension", rel_path, line,
                        f"registerExtension(\"{name}\") is not under a declared "
                        f"namespace {namespaces or '(none declared)'} -- another "
                        f"pack using the same name throws and silently drops one."))
            elif callee.endswith("customElements.define") or callee == "customElements.define" \
                    or callee.endswith(".define") and "customElements" in callee:
                name = _first_arg_name(node)
                if not _is_namespaced(name, namespaces):
                    findings.append(Finding(
                        name_sev, "unnamespaced-custom-element", rel_path, line,
                        f"customElements.define(\"{name}\") is not namespaced -- "
                        f"duplicate names throw across packs."))
            # --- message listeners: window.addEventListener("message", fn) ---
            elif callee in ("window.addEventListener", "addEventListener") \
                    or callee.endswith(".addEventListener"):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    real = [c for c in args.children if c.type not in ("(", ")", ",")]
                    if real and _string_literal_value(real[0]) == "message":
                        handler = real[1] if len(real) > 1 else None
                        if handler is not None and ".source" not in _text(handler):
                            findings.append(Finding(
                                "warn", "unguarded-message-listener", rel_path, line,
                                "window 'message' listener without an event.source "
                                "check -- fires for every pack's iframe messages, not "
                                "just yours."))

        # --- monkeypatch of a SHARED object's method (chaining unverifiable) ---
        # Only patches rooted at a shared global (LiteGraph, app, ...) collide;
        # overriding the node's own class prototype is the normal, safe pattern.
        if node.type == "assignment_expression":
            lhs = node.child_by_field_name("left")
            if lhs is not None and lhs.type == "member_expression":
                if _member_root(lhs) in _SHARED_PROTOTYPE_ROOTS:
                    findings.append(Finding(
                        "warn", "shared-object-monkeypatch", rel_path, line,
                        f"assigns {_text(lhs)} on a shared global -- if this "
                        f"overrides a method other packs rely on, capture and call "
                        f"the original (apply(this, arguments)) so you don't "
                        f"clobber them."))

    return findings


def lint_web_dir(web_dir: Path, namespaces: List[str],
                 namespaces_declared: bool) -> List[Finding]:
    """Lint every auto-imported .js under web_dir. .mjs is exempt (ComfyUI's
    glob is **/*.js, so .mjs never enters the main realm)."""
    findings: List[Finding] = []
    web_dir = Path(web_dir)
    for js in sorted(web_dir.rglob("*.js")):
        try:
            text = js.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = js.relative_to(web_dir).as_posix()
        findings.extend(lint_source(text, rel, namespaces, namespaces_declared))
    return findings
