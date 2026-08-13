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

The standard enforced is ISOLATION: until ComfyUI provides sanctioned hooks
for shared surfaces, a pack's main-realm JS may not touch anything it does
not own -- no window/globalThis writes (namespaced or not), no patches on
shared objects (LiteGraph/app/api), no appends into the shared document
(use the Popover API for overlays; keep elements in your own widget DOM),
no document-level listeners. What remains allowed is exactly the glue tier:
registerExtension under the pack's namespace, widget DOM inside the node,
iframes, and guarded pairwise postMessage.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

# Property names on window/globalThis that are the platform's, not a pack's --
# writing these is legitimate and must not be flagged.
_ALLOWED_GLOBAL_PROPS = {"__THREE__"}  # three.js's own multi-instance guard flag

# Identifiers that ARE the global object. `window`/`globalThis` unambiguously so.
_GLOBAL_OBJECTS = {"window", "globalThis"}
# ...and these, which resolve to the global object in the main realm too, but
# are ALSO common local-variable names (`const self = this`, a DOM `parent`, a
# scroll `top`). We only treat them as the global when the file never binds
# that name locally -- otherwise it's an ordinary variable and flagging it is
# a false positive.
_GLOBAL_OBJECT_ALIASES = {"self", "top", "parent", "frames"}

# Shared objects every pack sees. Patching a member on one of these can break
# every other pack. Overriding a member on the node's OWN class prototype
# (nodeType.prototype.onExecuted, the standard extension pattern) is per-node
# and safe, so it is NOT flagged -- only patches rooted at these globals are.
# Includes the JS builtins (prototype pollution) and ComfyUI's shared widget
# registry / graph objects.
_SHARED_PROTOTYPE_ROOTS = {
    "LiteGraph", "LGraphCanvas", "LGraphNode", "LGraph", "LGraphGroup",
    "ContextMenu", "app", "api", "ComfyWidgets", "ComfyApp",
    "Object", "Array", "String", "Number", "Boolean", "Function",
}

# document.<prop> = ... : writing one of these mutates shared page-wide state
# (cookies; the document's global stylesheet list) even though the LHS object
# is `document`, not `window`.
_SHARED_DOCUMENT_PROPS = {"cookie", "adoptedStyleSheets"}

# Shared per-origin key/value stores. An unprefixed key collides with every
# other pack's key of the same name.
_STORAGE_OBJECTS = {"localStorage", "sessionStorage"}
_STORAGE_METHODS = {"setItem", "getItem", "removeItem"}

# DOM-insertion methods: calling one of these with a callee rooted at
# `document` (document.body.appendChild, document.head.append,
# document.querySelector(...).appendChild, ...) injects into the SHARED
# document -- DOM the pack does not own.
_DOM_INSERT_METHODS = {"appendChild", "append", "prepend", "insertBefore",
                       "insertAdjacentElement", "insertAdjacentHTML", "replaceChildren"}

# Handler node types whose whole body we can see. If such a handler literally
# never references event.source/.origin, "unguarded" is a fact, not a guess ->
# error. A handler passed by reference (identifier / factory call) hides its
# body, so we can only warn.
_INLINE_FN_TYPES = {"arrow_function", "function", "function_expression",
                    "function_declaration", "generator_function",
                    "generator_function_declaration"}


def _member_root(node) -> Optional[str]:
    """Leftmost identifier of a member-expression chain (a.b.c -> 'a')."""
    while node is not None and node.type == "member_expression":
        node = node.child_by_field_name("object")
    if node is not None and node.type == "identifier":
        return node.text.decode("utf-8", errors="replace")
    return None


def _bound_names(root) -> set:
    """Identifiers the file binds as locals (declaration names + parameters +
    function/class names). Used to tell a real `const self = this` apart from a
    write to the global `self`. Over-collecting only makes us MISS a global
    write (conservative), never raise a false positive."""
    names = set()
    for n in _walk(root):
        if n.type == "variable_declarator":
            nm = n.child_by_field_name("name")
            if nm is not None and nm.type == "identifier":
                names.add(_text(nm))
        elif n.type in ("function_declaration", "generator_function_declaration",
                        "class_declaration"):
            nm = n.child_by_field_name("name")
            if nm is not None and nm.type == "identifier":
                names.add(_text(nm))
        elif n.type == "formal_parameters":
            for c in n.children:
                if c.type == "identifier":
                    names.add(_text(c))
        elif n.type == "arrow_function":
            p = n.child_by_field_name("parameter")  # single bare param: x => ...
            if p is not None and p.type == "identifier":
                names.add(_text(p))
    return names


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
    bound = _bound_names(tree.root_node)
    # global-object identifiers active in THIS file (aliases only count when the
    # file doesn't shadow them with a local binding).
    global_objs = _GLOBAL_OBJECTS | (_GLOBAL_OBJECT_ALIASES - bound)

    for node in _walk(tree.root_node):
        line = node.start_point[0] + 1

        # --- bare import specifier in an auto-imported main-realm file ---
        # `import * as THREE from 'three'` only resolves if an import map maps
        # it -- and pack import maps live in iframe HTML, not the main page.
        # Classic symptom of iframe-internal code leaking into the auto-import
        # glob (seen in the wild: ComfyUI-3D-Pack).
        if node.type == "import_statement":
            src = _string_literal_value(node.child_by_field_name("source"))
            if src is not None and not src.startswith((".", "/")):
                findings.append(Finding(
                    "warn", "unresolvable-bare-import", rel_path, line,
                    f"imports bare specifier '{src}' -- the main realm has no "
                    f"import map for it, so this file will throw when ComfyUI "
                    f"auto-imports it. If it is iframe-only code, rename it to "
                    f".mjs so it leaves the auto-import glob."))

        # --- global / shared-document writes ---
        if node.type == "assignment_expression":
            lhs = node.child_by_field_name("left")
            if lhs is not None and lhs.type == "member_expression":
                obj = lhs.child_by_field_name("object")
                prop = lhs.child_by_field_name("property")
                obj_text = _text(obj) if obj is not None else None
                # window.X = / globalThis.X = / (unshadowed) self|top|parent.X =.
                # Isolation standard: NO globals at all, namespaced or not --
                # module scope + iframes cover every legitimate need.
                if obj_text in global_objs and prop is not None:
                    pname = _text(prop)
                    if pname not in _ALLOWED_GLOBAL_PROPS:
                        findings.append(Finding(
                            "error", "global-write", rel_path, line,
                            f"writes {obj_text}.{pname} into the shared main realm "
                            f"(auto-imported .js). Keep state in module scope, or keep "
                            f"the code iframe-local (rename the file to .mjs so ComfyUI "
                            f"does not auto-import it)."))
                # document.cookie = / document.adoptedStyleSheets = : shared
                # page-wide state written through `document`, not `window`.
                elif obj_text == "document" and prop is not None \
                        and _text(prop) in _SHARED_DOCUMENT_PROPS:
                    findings.append(Finding(
                        "error", "shared-document-write", rel_path, line,
                        f"writes document.{_text(prop)} -- shared page-wide state. "
                        f"Keep per-pack state in module scope / your own storage key."))

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
            # --- document-level listeners: document(.body).addEventListener ---
            # The document is shared: N packs handling the same paste/keydown/
            # drop race each other. Listen on your OWN elements instead.
            elif callee.startswith("document.") and callee.endswith(".addEventListener"):
                findings.append(Finding(
                    "error", "document-level-listener", rel_path, line,
                    f"{callee}(...) attaches a listener to the shared document -- "
                    f"every pack's events flow through it. Attach the listener to "
                    f"an element your pack owns."))
            # --- DOM injection into the shared document ---
            # document.body.appendChild(...), document.head.append(...),
            # document.querySelector(...).appendChild(...): inserting into DOM
            # the pack does not own. For floating overlays use the Popover API
            # on an element inside your own widget DOM (top layer escapes the
            # canvas transform without touching document.body).
            elif callee.startswith("document.") and \
                    callee.rsplit(".", 1)[-1].split("?")[0] in _DOM_INSERT_METHODS:
                findings.append(Finding(
                    "error", "shared-dom-injection", rel_path, line,
                    f"{callee}(...) inserts into the shared document. Keep elements "
                    f"inside your own widget DOM; for floating overlays use the "
                    f"Popover API (showPopover renders in the top layer without "
                    f"appending to document.body)."))
            # --- message listeners: window.addEventListener("message", fn) ---
            elif callee in ("window.addEventListener", "addEventListener") \
                    or callee.endswith(".addEventListener"):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    real = [c for c in args.children if c.type not in ("(", ")", ",")]
                    if real and _string_literal_value(real[0]) == "message":
                        handler = real[1] if len(real) > 1 else None
                        if handler is not None and ".source" not in _text(handler) \
                                and ".origin" not in _text(handler):
                            if handler.type in _INLINE_FN_TYPES:
                                # whole body visible + no source/origin -> a fact
                                findings.append(Finding(
                                    "error", "unguarded-message-listener", rel_path, line,
                                    "window 'message' listener with no event.source / "
                                    "event.origin check -- it fires for EVERY pack's "
                                    "iframe messages, not just yours. Add "
                                    "'if (e.source !== myIframe.contentWindow) return;' "
                                    "at the top of the handler."))
                            else:
                                # handler passed by reference -- body hidden, can't be sure
                                findings.append(Finding(
                                    "warn", "unguarded-message-listener", rel_path, line,
                                    "window 'message' listener delegates to a named "
                                    "handler; make sure it checks event.source "
                                    "(can't verify statically)."))
            # --- postMessage to a SHARED receiver: message TYPE must be namespaced ---
            # Receiver-split (an AST-literal fact): posting to your own iframe's
            # `X.contentWindow` is pairwise delivery -- no other pack's listener
            # can ever receive it, so an unprefixed type there collides with
            # nothing. Only posts to shared receivers (window, parent, or an
            # unknown target) can land in other packs' listeners.
            elif callee.endswith(".postMessage") or callee == "postMessage":
                if ".contentWindow" in callee:
                    pass  # pairwise: cannot collide
                else:
                    args = node.child_by_field_name("arguments")
                    if args is not None:
                        real = [c for c in args.children if c.type not in ("(", ")", ",")]
                        if real and real[0].type == "object":
                            for pair in real[0].children:
                                if pair.type != "pair":
                                    continue
                                key = pair.child_by_field_name("key")
                                if key is None or _text(key).strip("\"'") != "type":
                                    continue
                                val = _string_literal_value(pair.child_by_field_name("value"))
                                if val is not None and not _is_namespaced(val, namespaces):
                                    sugg = f"{namespaces[0]}:{val}" if namespaces else f"<ns>:{val}"
                                    findings.append(Finding(
                                        name_sev, "unprefixed-message-type", rel_path, line,
                                        f"postMessage type '{val}' to a shared receiver "
                                        f"({callee.rsplit('.', 1)[0] or 'window'}) is not "
                                        f"namespaced -- prefix it (e.g. '{sugg}') so a "
                                        f"listener in another pack cannot match it."))
            # --- Object.assign / defineProperty on a shared target ---
            # The call-form of a global write / monkeypatch:
            # Object.assign(window, {...}), Object.defineProperty(LiteGraph, ...).
            elif callee in ("Object.assign", "Object.defineProperty",
                            "Object.defineProperties"):
                args = node.child_by_field_name("arguments")
                first = None
                if args is not None:
                    first = next((c for c in args.children
                                  if c.type not in ("(", ")", ",")), None)
                tgt = None
                if first is not None:
                    tgt = _text(first) if first.type == "identifier" \
                        else _member_root(first) if first.type == "member_expression" else None
                if tgt in global_objs:
                    findings.append(Finding(
                        "error", "global-write", rel_path, line,
                        f"{callee}({tgt}, ...) writes onto the shared global object."))
                elif tgt in _SHARED_PROTOTYPE_ROOTS:
                    findings.append(Finding(
                        "error", "shared-object-monkeypatch", rel_path, line,
                        f"{callee}({_text(first)}, ...) mutates a shared object other "
                        f"packs rely on -- nothing guarantees your change composes "
                        f"with theirs."))
            # --- localStorage / sessionStorage with an unprefixed key ---
            elif "." in callee and callee.rsplit(".", 1)[0] in _STORAGE_OBJECTS \
                    and callee.rsplit(".", 1)[1] in _STORAGE_METHODS:
                args = node.child_by_field_name("arguments")
                key = None
                if args is not None:
                    first = next((c for c in args.children
                                  if c.type not in ("(", ")", ",")), None)
                    key = _string_literal_value(first)
                if key is not None and not _is_namespaced(key, namespaces):
                    store = callee.rsplit(".", 1)[0]
                    sugg = f"{namespaces[0]}:{key}" if namespaces else f"<ns>:{key}"
                    findings.append(Finding(
                        name_sev, "unprefixed-storage-key", rel_path, line,
                        f"{store} key '{key}' is not namespaced -- it is shared per "
                        f"origin, so another pack's same-named key clobbers yours. "
                        f"Prefix it (e.g. '{sugg}')."))

        # --- BroadcastChannel("name") with an unprefixed channel name ---
        if node.type == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and _text(ctor) == "BroadcastChannel":
                args = node.child_by_field_name("arguments")
                chan = None
                if args is not None:
                    first = next((c for c in args.children
                                  if c.type not in ("(", ")", ",")), None)
                    chan = _string_literal_value(first)
                if chan is not None and not _is_namespaced(chan, namespaces):
                    sugg = f"{namespaces[0]}:{chan}" if namespaces else f"<ns>:{chan}"
                    findings.append(Finding(
                        name_sev, "unprefixed-broadcast-channel", rel_path, line,
                        f"BroadcastChannel('{chan}') is not namespaced -- every pack "
                        f"(and every tab) opening the same channel name shares the bus. "
                        f"Prefix it (e.g. '{sugg}')."))

        # --- monkeypatch of a SHARED object (LiteGraph, app, api, ...) ---
        # Isolation standard: patching objects every pack sees is not accepted
        # -- there is no way to guarantee two packs' patches compose. Only
        # overriding the node's OWN class prototype (nodeType.prototype.onX,
        # the standard extension pattern) is per-node and stays allowed.
        if node.type == "assignment_expression":
            lhs = node.child_by_field_name("left")
            if lhs is not None and lhs.type == "member_expression":
                if _member_root(lhs) in _SHARED_PROTOTYPE_ROOTS:
                    findings.append(Finding(
                        "error", "shared-object-monkeypatch", rel_path, line,
                        f"assigns {_text(lhs)} on a shared global -- other packs "
                        f"(and ComfyUI itself) rely on it, and nothing guarantees "
                        f"your patch composes with theirs. There is currently no "
                        f"safe way to do this from a pack."))

    return findings


def _relative_imports(text: str) -> List[str]:
    """Relative import/export specifiers in a module (./x.mjs, ../y.js)."""
    parser = _parser()
    tree = parser.parse(text.encode("utf-8"))
    out: List[str] = []
    for node in _walk(tree.root_node):
        if node.type in ("import_statement", "export_statement"):
            src = _string_literal_value(node.child_by_field_name("source"))
            if src is not None and src.startswith("."):
                out.append(src)
    return out


def lint_web_dir(web_dir: Path, namespaces: List[str],
                 namespaces_declared: bool) -> List[Finding]:
    """Lint every auto-imported .js under web_dir, PLUS any .mjs reachable by a
    relative import from those .js. ComfyUI's glob is **/*.js, so a bare .mjs
    never enters the main realm on its own -- but a .js that `import`s one pulls
    it in, so a leaking file cannot hide behind the .mjs extension."""
    findings: List[Finding] = []
    web_dir = Path(web_dir)

    js_files = sorted(web_dir.rglob("*.js"))
    scanned = set()               # resolved paths already linted
    reached_mjs: List[Path] = []  # .mjs pulled into the main realm via import

    def _follow(src_file: Path, text: str) -> None:
        for spec in _relative_imports(text):
            target = (src_file.parent / spec).resolve()
            if target.suffix == ".mjs" and target.is_file() and target not in scanned:
                scanned.add(target)
                reached_mjs.append(target)

    for js in js_files:
        try:
            text = js.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned.add(js.resolve())
        findings.extend(lint_source(text, js.relative_to(web_dir).as_posix(),
                                    namespaces, namespaces_declared))
        _follow(js, text)

    # BFS through .mjs -> .mjs imports as well.
    i = 0
    while i < len(reached_mjs):
        mjs = reached_mjs[i]
        i += 1
        try:
            text = mjs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = mjs.relative_to(web_dir).as_posix()
        except ValueError:
            rel = mjs.name
        findings.extend(lint_source(text, rel, namespaces, namespaces_declared))
        _follow(mjs, text)

    return findings
