"""HAZARDS level - opt-in report on how a pack behaves inside ComfyUI. Never fails.

WARNINGS asks whether the pack is laid out sanely. This one asks whether its code
misbehaves once ComfyUI imports it into a process it shares with ~200 other packs:
device handling, object ownership, import-time side effects, cancellation.

Findings are grouped into confidence bands, because the useful question for a
report-only level is not "how bad is this" but "if this fires, how sure am I that
something is actually wrong":

    A  presence proves it     -- no context needed; a hit is a finding
    B  near-certain           -- rests on one fact about how ComfyUI works
    C  worth a look           -- the pattern is real, the consequence varies
    D  judgement              -- a human decides every time

The bands are the product. A report that mixes "this crashes on every Mac" with
"this could be tidier" gets skimmed, and then the first one goes unread too. So
the band is printed with every finding and the order never changes.

Adding a check: put it in the band you can defend, not the band you want. If you
cannot say what a hit proves, it belongs in D or nowhere.

    # comfy-test.toml
    [test]
    levels = [..., "hazards"]
"""

import ast
import re
from pathlib import Path

from ..context import LevelContext

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "site-packages", "lib", "Lib", ".pixi", "scripts",
}

# Sockets carrying an object ComfyUI owns, caches, and hands to every downstream
# consumer of the link. Writing into one of these reaches other branches of the
# graph; writing into an IMAGE/INT/STRING does not.
SHARED_SOCKETS = {
    "MODEL", "VAE", "CLIP", "CLIP_VISION", "LATENT",
    "CONDITIONING", "CONTROL_NET", "STYLE_MODEL", "GLIGEN",
}

# torch.cuda members that are safe to reach for anywhere: they answer questions
# about CUDA rather than requiring it.
CUDA_SAFE = {"is_available", "device_count", "is_initialized", "is_built"}

# Distributions that install the same import name. Declaring two of them means
# whichever pip resolves last silently overwrites the other.
IMPORT_NAME_FAMILIES = {
    "cv2": {"opencv-python", "opencv-python-headless",
            "opencv-contrib-python", "opencv-contrib-python-headless"},
    "onnxruntime": {"onnxruntime", "onnxruntime-gpu", "onnxruntime-directml",
                    "onnxruntime-silicon", "onnxruntime-openvino"},
    "PIL": {"pillow", "pillow-simd"},
}

# cv2 namespaces that only exist in a contrib build.
CV2_CONTRIB_NS = ("ximgproc", "optflow", "xphoto", "face", "aruco",
                  "tracking", "img_hash", "wechat_qrcode", "bgsegm", "sfm")

# Names that mean "ComfyUI decides the dtype" -- a file consulting any of them
# is making a considered choice, whatever else it hardcodes.
DTYPE_HELPERS = ("should_use_fp16", "should_use_bf16", "unet_dtype",
                 "vae_dtype", "text_encoder_dtype", "supports_cast")

# Module roots whose attributes belong to ComfyUI, not to the pack.
COMFY_ROOTS = ("comfy", "comfy_extras", "folder_paths", "execution",
               "nodes", "model_management", "comfy_execution")

# A saved original under any of these names counts as restore discipline.
SAVE_PREFIXES = ("orig_", "original_", "previous_", "prev_", "backup_",
                 "_orig", "_original", "old_")

MUTATING_METHODS = {"update", "append", "extend", "insert", "pop", "clear",
                    "setdefault", "add", "discard", "remove", "sort"}

NETWORK_CALLS = ("urlopen", "urlretrieve", "hf_hub_download", "snapshot_download",
                 "get", "post", "download_url_to_file", "load_state_dict_from_url")
NETWORK_ROOTS = ("requests", "httpx", "urllib", "wget", "huggingface_hub")

WEIGHT_LOADERS = ("from_pretrained", "load_state_dict", "safe_open",
                  "load_file", "torch.load", "load_checkpoint")

MANAGED_MARKERS = ("ModelPatcher", "load_models_gpu", "model_management",
                   "comfy.sd.load", "load_checkpoint_guess_config", "mm.")

_ABS_DEVICE = re.compile(r'^cuda(:\d+)?$')
_FP16 = re.compile(r'dtype\s*=\s*torch\.(float16|bfloat16)|\.(half|bfloat16)\s*\(\s*\)')
_PIP_VERB = re.compile(r'\b(install|uninstall)\b')


# --------------------------------------------------------------------------
# walking / parsing
# --------------------------------------------------------------------------

def _py_files(node_dir):
    for path in node_dir.rglob("*.py"):
        rel = path.relative_to(node_dir)
        if any(p in SKIP_DIRS or p.startswith((".", "_env_")) for p in rel.parts):
            continue
        yield path, rel


def _trees(node_dir):
    """Parse every source file once. Unparseable files are skipped, not reported --
    SYNTAX already owns that verdict."""
    for path, rel in _py_files(node_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            yield rel, text, ast.parse(text)
        except SyntaxError:
            continue


def _dotted(node):
    """`a.b.c` -> "a.b.c" for Name/Attribute chains; None for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _root_name(node):
    """Leftmost Name of an attribute chain."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _import_time_nodes(tree):
    """Every node that executes when the module is imported.

    Prunes function bodies, since those run later; keeps class bodies and
    decorator expressions, since those run now. Callers must iterate this
    directly rather than ast.walk()ing what it yields -- walking a module-level
    `if` would descend back into the function bodies inside it, which is exactly
    how a scan ends up reporting import-time CUDA that lives in a method.
    """
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for decorator in getattr(child, "decorator_list", []):
                    yield decorator
                    yield from walk(decorator)
                continue
            yield child
            yield from walk(child)
    yield from walk(tree)


def _import_closure(node_dir):
    """Files reachable by following relative imports from __init__.py.

    Deliberately conservative: it cannot follow `importlib.import_module(name)`,
    so it under-approximates for the ~1/3 of packs that discover nodes by scan.
    Used only where "does this run at startup" is the question being asked.
    """
    init = node_dir / "__init__.py"
    if not init.exists():
        return set()

    seen, frontier = set(), [Path("__init__.py")]
    while frontier:
        rel = frontier.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = node_dir / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        pkg = rel.parent
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.ImportFrom) and node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.parent
                if node.module:
                    base = base.joinpath(*node.module.split("."))
                targets.append(base)
                # `from .pkg import mod` may name modules rather than symbols
                targets += [base / alias.name for alias in node.names]

            for target in targets:
                # A relative import can climb past the pack root (`from ....x`),
                # leaving Path("."), which has no name to give a suffix to.
                cands = [target / "__init__.py"]
                if target.name:
                    cands.append(target.with_suffix(".py"))
                for cand in cands:
                    if (node_dir / cand).exists():
                        frontier.append(cand)
    return seen


def _declared_requirements(node_dir):
    """Distribution names this pack declares, lowercased, from both manifests."""
    names = set()
    req = node_dir / "requirements.txt"
    if req.exists():
        try:
            for line in req.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if line and not line.startswith("-"):
                    names.add(re.split(r'[<>=!~\[; ]', line)[0].strip().lower())
        except (OSError, UnicodeDecodeError):
            pass

    pyproject = node_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            block = re.search(r'dependencies\s*=\s*\[(.*?)\]', text, re.S)
            if block:
                for raw in re.findall(r'["\']([^"\']+)["\']', block.group(1)):
                    names.add(re.split(r'[<>=!~\[; ]', raw)[0].strip().lower())
        except (OSError, UnicodeDecodeError):
            pass
    return names


# --------------------------------------------------------------------------
# BAND A -- presence proves it
# --------------------------------------------------------------------------

def _check_cpu_device_into_cuda_api(node_dir):
    """A device that can be "cpu" must not be handed to a torch.cuda.* call.

    This is the failure mode where the author wrote the CPU fallback themselves,
    one line above, and it made things worse: torch.cuda.memory_allocated() with
    a CPU device raises. Guaranteed crash on any Mac or --cpu install.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for scope in [tree] + [n for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            cpu_capable = set()
            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign):
                    continue
                # device = torch.device("cuda" if ... else "cpu")   /  ... if ... else "cpu"
                value = node.value
                if isinstance(value, ast.Call):
                    value = value.args[0] if value.args else value
                if not isinstance(value, ast.IfExp):
                    continue
                branches = [value.body, value.orelse]
                if not any(isinstance(b, ast.Constant) and b.value == "cpu" for b in branches):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        cpu_capable.add(target.id)

            if not cpu_capable:
                continue

            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted(node.func)
                if not dotted or not dotted.startswith("torch.cuda."):
                    continue
                if dotted.rsplit(".", 1)[-1] in CUDA_SAFE:
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in cpu_capable:
                        hits.append(f"    {rel}:{node.lineno}  {dotted}({arg.id}) "
                                    f"-- {arg.id} is 'cpu' when CUDA is absent")
    if not hits:
        return []
    return [f"{len(hits)} call(s) pass a CPU-capable device to a CUDA-only API:\n"
            + "\n".join(sorted(set(hits))[:10])]


def _check_cuda_at_import(node_dir):
    """torch.cuda.* at module scope kills the pack at startup, not the node at run.

    is_available()/device_count() are exempt: they answer a question about CUDA
    rather than requiring it.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for node in _import_time_nodes(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted(node.func)
                if not dotted or ".cuda." not in f".{dotted}.":
                    continue
                if not dotted.startswith(("torch.cuda.", "cuda.")):
                    continue
                if dotted.rsplit(".", 1)[-1] in CUDA_SAFE:
                    continue
                hits.append(f"    {rel}:{node.lineno}  {dotted}()")
    if not hits:
        return []
    return [f"{len(hits)} CUDA call(s) at import time -- these run when ComfyUI "
            f"loads the pack, and raise on a machine without CUDA:\n"
            + "\n".join(sorted(set(hits))[:10])]


# --------------------------------------------------------------------------
# BAND B -- near-certain
# --------------------------------------------------------------------------

def _socket_types(cls):
    """Map a node's execute-method parameter names to their declared socket types.

    Without this the check is useless: a generic "mutates a parameter" rule flags
    `sigmas[0] = 0.0001`, which is correct code, and buries the LATENT write that
    is not.
    """
    types = {}
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name != "INPUT_TYPES":
            continue
        for node in ast.walk(item):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if not isinstance(value, ast.Tuple) or not value.elts:
                    continue
                first = value.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    types[key.value] = first.value
    return types


def _function_name(cls):
    for item in cls.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if (isinstance(target, ast.Name) and target.id == "FUNCTION"
                        and isinstance(item.value, ast.Constant)):
                    return item.value.value
    return None


def _check_input_mutation(node_dir):
    """A node must not write into an object it received on a shared socket.

    ComfyUI hands the same object to every consumer of a link and propagates
    cached outputs by reference, so an in-place write reaches sibling branches
    and survives across queue runs. Core's own SetLatentNoiseMask does
    `s = samples.copy()` first; that copy is the whole fix.

    Rebinding the parameter anywhere in the method (`latent = latent.copy()`)
    counts as the copy and clears the finding.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            fname = _function_name(cls)
            sockets = _socket_types(cls)
            if not fname or not sockets:
                continue
            watched = {p for p, t in sockets.items() if t in SHARED_SOCKETS}
            if not watched:
                continue

            method = next((m for m in cls.body
                           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and m.name == fname), None)
            if method is None:
                continue

            params = {a.arg for a in method.args.args} | {a.arg for a in method.args.kwonlyargs}
            watched &= params
            # A rebind is a copy: `latent_image = latent_image.copy()`.
            rebound = {t.id for n in ast.walk(method) if isinstance(n, ast.Assign)
                       for t in n.targets if isinstance(t, ast.Name)}
            watched -= rebound
            if not watched:
                continue

            for node in ast.walk(method):
                target_nodes, verb = [], None
                if isinstance(node, ast.Assign):
                    target_nodes, verb = node.targets, "writes into"
                elif isinstance(node, ast.AugAssign):
                    target_nodes, verb = [node.target], "writes into"
                elif isinstance(node, ast.Delete):
                    # A bare `del name` drops a local binding and touches nothing
                    # the caller can see -- only Subscript/Attribute targets count.
                    target_nodes = [t for t in node.targets
                                    if isinstance(t, (ast.Subscript, ast.Attribute))]
                    verb = "deletes from"
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in MUTATING_METHODS:
                        target_nodes, verb = [node.func.value], "mutates"

                for target in target_nodes:
                    if not isinstance(target, (ast.Subscript, ast.Attribute)):
                        continue
                    root = _root_name(target.value if isinstance(target, ast.Subscript)
                                      else target)
                    if root in watched:
                        hits.append(f"    {rel}:{node.lineno}  {cls.name}.{fname} "
                                    f"{verb} `{root}` ({sockets[root]})")
    if not hits:
        return []
    return [f"{len(hits)} in-place write(s) to a shared input object. ComfyUI hands "
            f"the same object to every downstream node and caches it -- copy or "
            f"clone first:\n" + "\n".join(sorted(set(hits))[:10])]


def _check_conflicting_distributions(node_dir):
    """Two distributions that install the same import name silently overwrite."""
    declared = _declared_requirements(node_dir)
    findings = []
    for import_name, family in IMPORT_NAME_FAMILIES.items():
        clash = sorted(declared & family)
        if len(clash) > 1:
            findings.append(f"`{import_name}` is provided by {len(clash)} declared "
                            f"distributions: {', '.join(clash)}. Whichever pip resolves "
                            f"last wins, silently.")
    return findings


def _check_cv2_contrib(node_dir):
    """cv2.ximgproc / cv2.optflow do not exist outside a contrib build."""
    declared = _declared_requirements(node_dir)
    cv2_declared = declared & IMPORT_NAME_FAMILIES["cv2"]
    has_contrib = any("contrib" in name for name in cv2_declared)

    used, imports_cv2 = [], False
    for rel, text, _tree in _trees(node_dir):
        if re.search(r'^\s*(import cv2|from cv2)', text, re.M):
            imports_cv2 = True
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for ns in CV2_CONTRIB_NS:
                if f"cv2.{ns}" in line or f"from cv2.{ns}" in line:
                    used.append(f"    {rel}:{lineno}  cv2.{ns}")

    findings = []
    if used and not has_contrib:
        declared_txt = ", ".join(sorted(cv2_declared)) or "nothing"
        findings.append(f"{len(used)} use(s) of a contrib-only cv2 namespace while "
                        f"declaring {declared_txt}. These attributes do not exist in a "
                        f"non-contrib build:\n" + "\n".join(sorted(set(used))[:6]))
    if imports_cv2 and not cv2_declared:
        findings.append("imports cv2 but declares no OpenCV distribution. ComfyUI core "
                        "ships none, so this only works when another installed pack "
                        "happens to pull one in.")
    return findings


def _check_bare_pip(node_dir):
    """`pip` from PATH is not necessarily this interpreter's pip."""
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.List) or not arg.elts:
                    continue
                first = arg.elts[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                if first.value.split("/")[-1] not in ("pip", "pip3"):
                    continue
                rest = " ".join(e.value for e in arg.elts[1:]
                                if isinstance(e, ast.Constant) and isinstance(e.value, str))
                if _PIP_VERB.search(rest):
                    hits.append(f"    {rel}:{node.lineno}  ['{first.value}', {rest!r}...]")
    if not hits:
        return []
    return [f"{len(hits)} call(s) invoke bare `pip` rather than "
            f"[sys.executable, '-m', 'pip', ...]. On a portable or venv install that "
            f"is a different interpreter's site-packages:\n" + "\n".join(sorted(set(hits))[:6])]


def _check_unconditional_cuda_literal(node_dir):
    """A hardcoded "cuda" with no fallback anywhere in the enclosing scope.

    ComfyUI owns device selection (--cpu, --cuda-device N, MPS, XPU, DirectML).
    A pack that is genuinely CUDA-only is a legitimate exception, which is why
    this reports rather than fails.
    """
    guard_words = ("is_available", "startswith", "cpu", "mps", "xpu", "directml",
                   "get_torch_device", "device_count")
    hits = []
    for rel, text, tree in _trees(node_dir):
        for scope in [tree] + [n for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            try:
                scope_src = ast.get_source_segment(text, scope) or text
            except Exception:
                scope_src = text
            if any(word in scope_src for word in guard_words):
                continue

            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign):
                    continue
                value = node.value
                if isinstance(value, ast.Call):
                    dotted = _dotted(value.func) or ""
                    if not dotted.endswith("device"):
                        continue
                    value = value.args[0] if value.args else None
                if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    continue
                if not _ABS_DEVICE.match(value.value):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not names:
                    continue
                hits.append(f"    {rel}:{node.lineno}  {names[0]} = \"{value.value}\"")
    if not hits:
        return []
    return [f"{len(hits)} unconditional CUDA device literal(s) with no fallback in "
            f"scope. Use comfy.model_management.get_torch_device(), or declare the "
            f"pack CUDA-only:\n" + "\n".join(sorted(set(hits))[:10])]


# --------------------------------------------------------------------------
# BAND C -- worth a look
# --------------------------------------------------------------------------

def _check_import_time_network(node_dir):
    """Network fetches and pip installs that run when ComfyUI imports the pack.

    Scoped to files reachable from __init__.py, because "at import" is the whole
    claim. A startup that blocks on someone else's HTTP server is a startup that
    can hang.
    """
    reachable = _import_closure(node_dir)
    if not reachable:
        return []

    hits = []
    for rel, _text, tree in _trees(node_dir):
        if rel not in reachable:
            continue
        for node in _import_time_nodes(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted(node.func) or ""
                tail = dotted.rsplit(".", 1)[-1]
                root = dotted.split(".")[0]

                literals = " ".join(a.value for a in node.args
                                    if isinstance(a, ast.Constant) and isinstance(a.value, str))
                for arg in node.args:
                    if isinstance(arg, ast.List):
                        literals += " " + " ".join(
                            e.value for e in arg.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str))

                if "pip" in literals and _PIP_VERB.search(literals):
                    hits.append(f"    {rel}:{node.lineno}  pip install at import")
                elif tail in NETWORK_CALLS and root in NETWORK_ROOTS:
                    hits.append(f"    {rel}:{node.lineno}  {dotted}() at import")
    if not hits:
        return []
    return [f"{len(hits)} network or install call(s) during import, reachable from "
            f"__init__.py. ComfyUI's startup blocks on these:\n"
            + "\n".join(sorted(set(hits))[:10])]


def _check_unrestored_monkeypatch(node_dir):
    """Rewriting a ComfyUI attribute without ever saving the original.

    One process, many packs: whatever is replaced here stays replaced for core
    and for every other pack. Saving the original anywhere in the pack clears the
    finding -- most packs that patch do restore, and flagging them is noise.
    """
    patched, saved_somewhere = [], False
    for rel, text, tree in _trees(node_dir):
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in COMFY_ROOTS:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in COMFY_ROOTS:
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        if any(f"{prefix}" in text for prefix in SAVE_PREFIXES):
            saved_somewhere = True

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                root = _root_name(target)
                if root not in aliases:
                    continue
                if any(target.attr.startswith(p) or p in target.attr for p in SAVE_PREFIXES):
                    saved_somewhere = True
                    continue
                dotted = _dotted(target) or target.attr
                hits_scope = "at import" if node.col_offset == 0 else ""
                patched.append(f"    {rel}:{node.lineno}  {dotted} {hits_scope}".rstrip())

    if not patched or saved_somewhere:
        return []
    return [f"{len(patched)} assignment(s) to a ComfyUI attribute, with no saved "
            f"original anywhere in the pack. This replacement outlives the node for "
            f"core and for every other installed pack:\n"
            + "\n".join(sorted(set(patched))[:10])]


def _check_swallowed_registration(node_dir):
    """A pack that registers nothing but reports IMPORT OK.

    ComfyUI prints success as long as the import does not raise, so a bare except
    around the mappings write turns a broken pack into "node type not found" with
    nothing naming the culprit.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            writes_mappings = any(
                isinstance(inner, ast.Name) and "NODE_CLASS_MAPPINGS" in inner.id
                for stmt in node.body for inner in ast.walk(stmt))
            if not writes_mappings:
                continue
            for handler in node.handlers:
                broad = handler.type is None or (
                    isinstance(handler.type, ast.Name)
                    and handler.type.id in ("Exception", "BaseException"))
                silent = all(isinstance(s, (ast.Pass, ast.Expr)) for s in handler.body)
                if broad and silent:
                    hits.append(f"    {rel}:{handler.lineno}  "
                                f"except {'' if handler.type is None else 'Exception'}: "
                                f"around the NODE_CLASS_MAPPINGS write")
    if not hits:
        return []
    return [f"{len(hits)} broad except around node registration. If this fires the "
            f"pack registers nothing and ComfyUI still logs IMPORT OK:\n"
            + "\n".join(sorted(set(hits))[:6])]


def _check_uncancellable_loop(node_dir):
    """A loop that drives a ProgressBar but never checks for an interrupt.

    The ProgressBar is the author stating the loop is long-running. Without
    throw_exception_if_processing_interrupted() the UI's cancel button does
    nothing until the node returns, with VRAM held throughout.

    Any use of the interrupt helper anywhere in the pack clears every finding --
    the check cannot see a call one stack frame down.
    """
    texts = {rel: text for rel, text, _ in _trees(node_dir)}
    if any("throw_exception_if_processing_interrupted" in t for t in texts.values()):
        return []

    hits = []
    for rel, _text, tree in _trees(node_dir):
        for scope in [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            bars = {t.id for n in ast.walk(scope) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)
                    and isinstance(n.value, ast.Call)
                    and "ProgressBar" in (_dotted(n.value.func) or "")}
            if not bars:
                continue
            for node in ast.walk(scope):
                if not isinstance(node, (ast.For, ast.While)):
                    continue
                # A constant-bounded short loop is not what this is about.
                if isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
                    if (_dotted(node.iter.func) == "range" and len(node.iter.args) == 1
                            and isinstance(node.iter.args[0], ast.Constant)
                            and isinstance(node.iter.args[0].value, int)
                            and node.iter.args[0].value < 8):
                        continue
                drives_bar = any(
                    isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "update"
                    and _root_name(inner.func) in bars
                    for stmt in node.body for inner in ast.walk(stmt))
                if drives_bar:
                    hits.append(f"    {rel}:{node.lineno}  in {scope.name}()")
    if not hits:
        return []
    return [f"{len(hits)} loop(s) drive a ProgressBar with no interrupt check, and "
            f"the pack never calls throw_exception_if_processing_interrupted(). "
            f"Cancel will not work until the node returns:\n"
            + "\n".join(sorted(set(hits))[:10])]


# --------------------------------------------------------------------------
# BAND D -- judgement
# --------------------------------------------------------------------------

def _check_hardcoded_precision(node_dir):
    """fp16/bf16 chosen by the pack rather than by ComfyUI.

    Under --cpu this raises "addmm_impl_cpu_ not implemented for 'Half'"; bf16
    dies on pre-Ampere; and it overrides --force-fp32. Files that consult any
    model_management dtype helper are exempt -- they are making a choice.
    """
    hits = []
    for rel, text, _tree in _trees(node_dir):
        if any(helper in text for helper in DTYPE_HELPERS):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or " if " in line:
                continue
            if _FP16.search(line):
                hits.append(f"    {rel}:{lineno}  {stripped[:70]}")
    if not hits:
        return []
    return [f"{len(hits)} hardcoded fp16/bf16 site(s) in files that never consult "
            f"model_management. Quantization code legitimately does this:\n"
            + "\n".join(sorted(set(hits))[:10])]


def _check_cache_clear_in_loop(node_dir):
    """empty_cache()/gc.collect() per iteration.

    empty_cache() frees the whole cached pool, which implicitly synchronizes the
    device and forces fresh cudaMallocs. It also invalidates the reserved-pool
    figures model_management uses to size offload decisions.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.While)):
                continue
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if not isinstance(inner, ast.Call):
                        continue
                    dotted = _dotted(inner.func) or ""
                    tail = dotted.rsplit(".", 1)[-1]
                    if tail in ("empty_cache", "soft_empty_cache") or dotted == "gc.collect":
                        hits.append(f"    {rel}:{inner.lineno}  {dotted}() inside a loop")
    if not hits:
        return []
    return [f"{len(hits)} allocator-wide free(s) inside a loop:\n"
            + "\n".join(sorted(set(hits))[:10])]


def _check_import_time_global_mutation(node_dir):
    """Process-wide state changed when the pack is imported.

    One process, many packs. logging.basicConfig() attaches a handler to the root
    logger and reformats every ComfyUI log line; set_float32_matmul_precision()
    changes fp32 accuracy for core sampling. None of it is ever restored.
    """
    hits = []
    for rel, _text, tree in _trees(node_dir):
        for node in _import_time_nodes(tree):
                if isinstance(node, ast.Call):
                    dotted = _dotted(node.func) or ""
                    if dotted in ("logging.basicConfig", "warnings.filterwarnings",
                                  "torch.set_float32_matmul_precision", "torch.manual_seed",
                                  "torch.set_grad_enabled", "matplotlib.use", "os.chdir",
                                  "os.environ.setdefault"):
                        hits.append(f"    {rel}:{node.lineno}  {dotted}()")
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript):
                            root = _dotted(target.value) or ""
                            if root in ("os.environ", "sys.modules"):
                                hits.append(f"    {rel}:{node.lineno}  {root}[...] = ...")
                        elif isinstance(target, ast.Attribute):
                            dotted = _dotted(target) or ""
                            if dotted.startswith("torch.backends."):
                                hits.append(f"    {rel}:{node.lineno}  {dotted} = ...")
    if not hits:
        return []
    return [f"{len(hits)} process-wide mutation(s) at import. These apply to ComfyUI "
            f"core and to every other installed pack, permanently:\n"
            + "\n".join(sorted(set(hits))[:10])]


def _check_unmanaged_model_load(node_dir):
    """Weights moved to GPU with no sign of ComfyUI's memory management anywhere.

    Deliberately coarse and deliberately last. Plenty of packs legitimately load
    auxiliary models -- face detectors, depth nets, upscalers -- that ModelPatcher
    was never meant to wrap, and this cannot tell those apart. It answers "is this
    pack invisible to the offloader", which is a question, not a verdict.
    """
    loads, moves_to_gpu, managed = [], False, False
    for rel, text, _tree in _trees(node_dir):
        if any(marker in text for marker in MANAGED_MARKERS):
            managed = True
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(loader in line for loader in WEIGHT_LOADERS):
                loads.append(f"    {rel}:{lineno}")
            if re.search(r'\.to\(\s*(["\']cuda|device|torch\.device)', line):
                moves_to_gpu = True

    if managed or not loads or not moves_to_gpu:
        return []
    return [f"loads weights at {len(loads)} site(s) and moves them to a device, but "
            f"the pack never mentions ModelPatcher, load_models_gpu or "
            f"model_management. VRAM held this way is invisible to ComfyUI's "
            f"offloader -- though auxiliary models legitimately live outside it:\n"
            + "\n".join(sorted(set(loads))[:6])]


# --------------------------------------------------------------------------

BAND_A = "A -- presence proves it"
BAND_B = "B -- near-certain"
BAND_C = "C -- worth a look"
BAND_D = "D -- judgement"

CHECKS = [
    (BAND_A, "cpu-into-cuda", "a CPU-capable device reaches a CUDA-only API",
     _check_cpu_device_into_cuda_api),
    (BAND_A, "cuda-at-import", "torch.cuda.* at module scope",
     _check_cuda_at_import),

    (BAND_B, "input-mutation", "in-place write to a shared input object",
     _check_input_mutation),
    (BAND_B, "dist-conflict", "two distributions, one import name",
     _check_conflicting_distributions),
    (BAND_B, "cv2-contrib", "cv2 usage the manifest does not support",
     _check_cv2_contrib),
    (BAND_B, "bare-pip", "pip invoked from PATH, not sys.executable",
     _check_bare_pip),
    (BAND_B, "cuda-literal", "hardcoded CUDA device with no fallback",
     _check_unconditional_cuda_literal),

    (BAND_C, "import-network", "network or pip install during import",
     _check_import_time_network),
    (BAND_C, "unrestored-patch", "ComfyUI attribute replaced, never saved",
     _check_unrestored_monkeypatch),
    (BAND_C, "swallowed-registration", "broad except around node registration",
     _check_swallowed_registration),
    (BAND_C, "uncancellable-loop", "long loop with no interrupt check",
     _check_uncancellable_loop),

    (BAND_D, "hardcoded-precision", "fp16/bf16 not chosen by model_management",
     _check_hardcoded_precision),
    (BAND_D, "cache-clear-in-loop", "allocator-wide free inside a loop",
     _check_cache_clear_in_loop),
    (BAND_D, "import-global-mutation", "process-wide state changed at import",
     _check_import_time_global_mutation),
    (BAND_D, "unmanaged-model", "weights on GPU outside ComfyUI's management",
     _check_unmanaged_model_load),
]


def run(ctx: LevelContext) -> LevelContext:
    """Run every hazard check, grouped by confidence band. Never raises."""
    total = 0
    current_band = None

    for band, name, description, check in CHECKS:
        try:
            findings = check(ctx.node_dir)
        except Exception as exc:  # a report-only check must never break a run
            ctx.log(f"[hazards] {name}: check itself failed ({type(exc).__name__}: {exc})")
            continue
        if not findings:
            continue

        if band != current_band:
            ctx.log(f"\n=== {band} ===")
            current_band = band
        total += len(findings)
        ctx.log(f"\n[hazards] {name} -- {description}")
        for finding in findings:
            ctx.log(f"  {finding}")

    if total == 0:
        ctx.log("Hazards check: clean")
    else:
        ctx.log(f"\nHazards check: {total} finding(s). None of these fail the build. "
                f"Band A findings need no judgement; band D findings need a human.")
    return ctx
