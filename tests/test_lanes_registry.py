"""Guard the platform registry as the single source of truth.

Everything lane-related derives from comfy_test.lanes.registry; these
tests fail fast if a consumer drifts out of sync with it. Runs under pytest or
as a plain script (`python tests/test_lanes_registry.py`).
"""

import comfy_test.lanes as P
from comfy_test.common.config import TestConfig
from comfy_test.reporting.html_report import LANES as HP


def test_every_id_resolves_to_itself():
    for p in P.LANES:
        assert P.resolve(p.id) is p, p.id


def test_config_keys_match_testconfig_fields():
    # get_platform_config() does getattr(config, platform.config_key); every
    # config_key must therefore be a real TestConfig field.
    fields = set(TestConfig(name="x").__dataclass_fields__)
    for p in P.LANES:
        assert p.config_key in fields, f"{p.id} -> {p.config_key} not a TestConfig field"


def test_html_gallery_derives_from_registry():
    assert HP == [{"id": p.id, "label": p.label} for p in P.LANES]


def test_backends_are_concrete_no_gpu():
    for p in P.LANES:
        assert p.backend in ("cpu", "cuda", "rocm"), p.id
        assert "gpu" not in p.id


def test_hosted_iff_cpu():
    # cpu platforms run on GitHub-hosted runners (test-matrix); cuda platforms
    # are dispatch-only (self-hosted via dispatch-test). desktop-cpu is hosted.
    for p in P.LANES:
        assert p.hosted == (p.backend == "cpu"), p.id


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all registry invariants hold")
