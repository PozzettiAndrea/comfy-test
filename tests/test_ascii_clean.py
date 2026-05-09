"""Source files must be pure ASCII so prints don't crash on Windows cp1252 stdout."""
import pathlib
import re

NON_ASCII = re.compile(rb'[^\x00-\x7F]')


def test_source_is_pure_ascii():
    src = pathlib.Path(__file__).parent.parent / "src" / "comfy_test"
    bad = []
    for path in src.rglob("*.py"):
        data = path.read_bytes()
        for lineno, line in enumerate(data.splitlines(), 1):
            if NON_ASCII.search(line):
                rel = path.relative_to(src)
                bad.append(f"{rel}:{lineno}: {line!r}")
    assert not bad, "non-ASCII chars in source:\n  " + "\n  ".join(bad)
