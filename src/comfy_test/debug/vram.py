"""VRAM/RAM debug logging for ComfyUI.

Activated via COMFY_VRAM_DEBUG=1 env var. Monkey-patches ComfyUI's
model_management to log VRAM state at every model load/unload transition.

Usage (automatic via comfy-test --vram-debug):
    A sitecustomize.py is dropped into the test venv that calls
    install_import_hook(). When comfy.model_management is imported,
    key functions are wrapped with VRAM state logging.

Usage (manual):
    Set COMFY_VRAM_DEBUG=1, then in Python:
        from comfy_test.debug.vram import install_import_hook
        install_import_hook()
"""

import importlib
import os
import sys

_TAG = "[VRAM]"
_installed = False


def log_vram_state(label, model_patchers=None):
    """Log current VRAM/RAM state.

    Args:
        label: Description of the current point (e.g. "After load_models_gpu")
        model_patchers: Optional list of ModelPatcher instances to show module breakdown
    """
    import torch
    import comfy.model_management as mm

    print(file=sys.stderr)
    print(f"{_TAG} {'=' * 10} {label} {'=' * 10}", file=sys.stderr, flush=True)

    # GPU stats
    dev = mm.get_torch_device()
    if dev.type == "cuda":
        total = mm.get_total_memory(dev) // (1024 * 1024)
        free = mm.get_free_memory(dev) // (1024 * 1024)
        used = total - free

        # PyTorch allocator stats
        try:
            stats = torch.cuda.memory_stats(dev)
            reserved = stats.get("reserved_bytes.all.current", 0) // (1024 * 1024)
            active = stats.get("active_bytes.all.current", 0) // (1024 * 1024)
            pt_info = f" | PyTorch: {reserved} MB reserved, {active} MB active"
        except Exception:
            pt_info = ""

        print(f"{_TAG} GPU: {used} / {total} MB{pt_info}", file=sys.stderr)
    else:
        print(f"{_TAG} Device: {dev} (no GPU stats)", file=sys.stderr)

    # RAM stats
    try:
        ram_free = mm.get_free_ram() // (1024 * 1024)
        print(f"{_TAG} RAM free: {ram_free} MB", file=sys.stderr)
    except Exception:
        pass

    # Loaded models
    loaded = mm.current_loaded_models
    if loaded:
        print(f"{_TAG} Loaded models ({len(loaded)}):", file=sys.stderr)
        for lm in loaded:
            name = lm.model.model.__class__.__name__
            gpu_mb = lm.model_loaded_memory() // (1024 * 1024)
            off_mb = lm.model_offloaded_memory() // (1024 * 1024)
            model_obj = lm.model.model
            is_lowvram = getattr(model_obj, "model_lowvram", False)
            patches = getattr(model_obj, "lowvram_patch_counter", 0)
            if is_lowvram:
                mode = f"partial ({patches} patches)"
            else:
                mode = "full"
            print(
                f"{_TAG}   {name:<30s} {gpu_mb:>6} MB GPU | {off_mb:>6} MB offloaded | {mode}",
                file=sys.stderr,
            )
    else:
        print(f"{_TAG} Loaded models: (none)", file=sys.stderr)

    # Module breakdown for requested patchers
    if model_patchers:
        for patcher in model_patchers:
            _log_module_breakdown(patcher)

    print(file=sys.stderr)
    sys.stderr.flush()


def _log_module_breakdown(patcher):
    """Log per-module device and size for a ModelPatcher."""
    model = patcher.model
    name = model.__class__.__name__
    print(f"{_TAG} --- Module breakdown: {name} ---", file=sys.stderr)

    _log_children(model, depth=1, max_depth=4)


def _log_children(module, depth, max_depth):
    """Recursively log children, expanding any submodule >100 MB."""
    indent = "  " * depth
    width = 30 - (depth - 1) * 2  # shrink label width as indent grows
    for child_name, child_mod in module.named_children():
        size_mb = sum(p.nbytes for p in child_mod.parameters()) // (1024 * 1024)
        device = _get_module_device(child_mod)
        print(f"{_TAG} {indent}{child_name:<{width}s} {size_mb:>6} MB  {device}", file=sys.stderr)

        if size_mb > 100 and depth < max_depth:
            _log_children(child_mod, depth + 1, max_depth)


def _get_module_device(module):
    """Get the device of a module's first parameter."""
    try:
        return next(module.parameters()).device
    except StopIteration:
        return "no params"


# ---------------------------------------------------------------------------
# Import hook: patches comfy.model_management when it's first imported
# ---------------------------------------------------------------------------

class _VramDebugFinder:
    """sys.meta_path finder that patches comfy.model_management after import."""

    def find_module(self, fullname, path=None):
        if fullname == "comfy.model_management":
            return self
        return None

    def load_module(self, fullname):
        # Remove ourselves to avoid recursion
        sys.meta_path.remove(self)
        # Let the real import happen
        mod = importlib.import_module(fullname)
        sys.modules[fullname] = mod
        _patch_model_management(mod)
        _patch_model_patcher()
        return mod


def install_import_hook():
    """Install a sys.meta_path hook that patches comfy.model_management on import.

    Safe to call multiple times (idempotent).
    """
    global _installed
    if _installed:
        return
    _installed = True

    # If already imported, patch directly
    if "comfy.model_management" in sys.modules:
        import comfy.model_management as mm
        _patch_model_management(mm)
        _patch_model_patcher()
    else:
        sys.meta_path.insert(0, _VramDebugFinder())

    print(f"{_TAG} VRAM debug hooks installed (COMFY_VRAM_DEBUG=1)", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Monkey-patches
# ---------------------------------------------------------------------------

def _patch_model_management(mm):
    """Wrap load_models_gpu() and free_memory() with VRAM logging."""
    original_load = mm.load_models_gpu
    original_free = mm.free_memory

    def load_models_gpu_wrapper(models, *args, **kwargs):
        model_names = ", ".join(m.model.__class__.__name__ for m in models)
        log_vram_state(f"Before load_models_gpu ({model_names})")
        result = original_load(models, *args, **kwargs)
        log_vram_state(f"After load_models_gpu ({model_names})", model_patchers=models)
        return result

    def free_memory_wrapper(memory_required, *args, **kwargs):
        result = original_free(memory_required, *args, **kwargs)
        if result:  # Only log if something was actually unloaded
            names = ", ".join(lm.model.model.__class__.__name__ for lm in result)
            log_vram_state(f"After free_memory (unloaded: {names})")
        return result

    mm.load_models_gpu = load_models_gpu_wrapper
    mm.free_memory = free_memory_wrapper


def _patch_model_patcher():
    """Wrap ModelPatcher.load() and partially_unload() with VRAM logging."""
    import comfy.model_patcher as mp

    original_load = mp.ModelPatcher.load
    original_partial_unload = mp.ModelPatcher.partially_unload

    def load_wrapper(self, *args, **kwargs):
        result = original_load(self, *args, **kwargs)
        name = self.model.__class__.__name__
        loaded_mb = self.loaded_size() // (1024 * 1024)
        total_mb = self.model_size() // (1024 * 1024)
        offloaded_mb = total_mb - loaded_mb
        patches = self.lowvram_patch_counter()
        if offloaded_mb > 0:
            mode = f"partial ({patches} patches)"
        else:
            mode = "full"
        print(
            f"{_TAG} ModelPatcher.load: {name} -> {loaded_mb} MB loaded, "
            f"{offloaded_mb} MB offloaded [{mode}]",
            file=sys.stderr, flush=True,
        )
        return result

    def partial_unload_wrapper(self, *args, **kwargs):
        before = self.loaded_size() // (1024 * 1024)
        result = original_partial_unload(self, *args, **kwargs)
        after = self.loaded_size() // (1024 * 1024)
        name = self.model.__class__.__name__
        freed = before - after
        print(
            f"{_TAG} ModelPatcher.partially_unload: {name} freed {freed} MB "
            f"({before} -> {after} MB loaded)",
            file=sys.stderr, flush=True,
        )
        return result

    mp.ModelPatcher.load = load_wrapper
    mp.ModelPatcher.partially_unload = partial_unload_wrapper


# ---------------------------------------------------------------------------
# sitecustomize.py content for injection into test venvs
# ---------------------------------------------------------------------------

_PTH_CONTENT = (
    "import os; "
    "exec(\"if os.environ.get('COMFY_VRAM_DEBUG'):"
    "\\n    from comfy_test.debug.vram import install_import_hook"
    "\\n    install_import_hook()\")\n"
)


def get_pth_content():
    """Return the content for a .pth file to inject into a test venv.

    Unlike sitecustomize.py (which is loaded from system site-packages,
    not the venv), .pth files ARE processed from venv site-packages.
    """
    return _PTH_CONTENT
