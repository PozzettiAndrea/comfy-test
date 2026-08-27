"""FORBIDDEN_PATTERNS must match torch.nn and nothing that merely looks like it.

The nn.* rules were unanchored and fired on any identifier ending in "nn" and
on any library exposing an `nn` submodule -- torchsparse.nn.Conv3d being the
real-world case, a sparse-conv layer with no comfy.ops equivalent. That is a
false positive a node author can only silence by renaming their imports, so it
gets a test.
"""

from comfy_test.orchestration.levels.syntax import (
    FORBIDDEN_PATTERNS,
    WARNING_PATTERNS,
)


def _hits(line):
    return [desc for pat, desc in FORBIDDEN_PATTERNS if pat.search(line)]


def _warns(line):
    return [desc for pat, desc in WARNING_PATTERNS if pat.search(line)]


FLAGGED = [
    "self.fc = nn.Linear(8, 4)",
    "self.fc = torch.nn.Linear(8, 4)",
    "        self.c = nn.Conv2d(3, 8, 1)",
    "self.c = torch.nn.Conv3d(3, 8, 1)",
    "self.n = nn.LayerNorm(8)",
    "self.n = torch.nn.GroupNorm(2, 8)",
    "self.e = nn.Embedding(10, 4)",
    "self.u = nn.ConvTranspose2d(8, 3, 2)",
]

FLAGGED_HUB = [
    'self.model = torch.hub.load("facebookresearch/dinov2", model_type)',
    "torch.hub.download_url_to_file(url, dst)",
    "sd = torch.hub.load_state_dict_from_url(u)",
    "torch.hub.set_dir(cache)",
]


def test_torch_hub_is_forbidden():
    for line in FLAGGED_HUB:
        assert _hits(line), f"should flag: {line}"


def test_hub_named_things_are_not_flagged():
    # an attribute or module merely named hub is not torch.hub
    for line in ("y = myhub.load(x)", "from hub import thing", "self.hub.load(x)"):
        assert not _hits(line), f"false positive: {line}"


NOT_FLAGGED = [
    # different library, no comfy.ops equivalent
    "self.conv = torchsparse.nn.Conv3d(a, b, k, s, 0, d, bias)",
    # any alias ending in "nn"
    "self.conv = spnn.Conv3d(a, b)",
    "y = mynn.Linear(4, 4)",
    "z = cudnn.Conv2d(1, 1, 1)",
    # attribute access, not construction
    "isinstance(m, nn.Linear)",
    # module reference without a call
    "layer_cls = nn.Linear",
]


def test_flags_real_torch_nn_layers():
    for line in FLAGGED:
        assert _hits(line), f"should have been flagged: {line!r}"


def test_does_not_flag_lookalikes():
    for line in NOT_FLAGGED:
        assert not _hits(line), f"false positive: {line!r} -> {_hits(line)}"


def test_device_and_autocast_still_flagged():
    for line in [
        "x = x.cuda()",
        'x = x.to("cuda")',
        'x = x.to(torch.device("cuda"))',
        "with torch.autocast('cuda'):",
        "with torch.cuda.amp.autocast():",
        "with torch.amp.autocast('cuda'):",
    ]:
        assert _hits(line), f"should have been flagged: {line!r}"


def test_empty_cache_warns_but_does_not_fail():
    """torch.cuda.empty_cache() is correct on CUDA and a no-op everywhere else.

    Worth surfacing -- soft_empty_cache also covers MPS/XPU/NPU/MLU and adds
    synchronize() + ipc_collect() -- but not worth failing a build over, so it
    belongs in WARNING_PATTERNS and must stay out of FORBIDDEN_PATTERNS.
    """
    line = "        torch.cuda.empty_cache()"
    assert _warns(line), "should warn"
    assert not _hits(line), "must not fail the build"


def test_soft_empty_cache_is_not_flagged():
    assert not _warns("comfy.model_management.soft_empty_cache()")
    assert not _hits("comfy.model_management.soft_empty_cache()")
