"""The lane taxonomy: the (os x backend x install_method) combinations
comfy-test can run a pack against.

A *lane* is one such combination -- `linux-cuda`, `windows-portable-cpu`,
`macos-desktop`. Ten of them. `registry.py` is the single source of truth;
everything else derives from it.

Deliberately NOT called "platform": that word is reserved for `sys.platform`
and wheel tags (`win_amd64`, `manylinux_2_35_x86_64`), which name only ONE
component of a lane id. Per-OS behaviour implementations live in
`comfy_test.platforms`, which is the OS sense of the word.
"""

from .registry import (
    Lane,
    LANES,
    BY_ID,
    resolve,
    allowed_tokens,
    gallery_lanes,
    matrix,
)

__all__ = [
    "Lane",
    "LANES",
    "BY_ID",
    "resolve",
    "allowed_tokens",
    "gallery_lanes",
    "matrix",
]
