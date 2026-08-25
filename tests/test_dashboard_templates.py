"""Static guards for the published dashboard templates.

The dashboard is three nested iframes that talk over postMessage. Both of the
failure modes below are COMPLETELY SILENT at runtime -- no exception, no
console error, no visual cue -- so a browser test would not catch them either.
These run in milliseconds with no browser and no server.

Runs under pytest or as a plain script (`python tests/test_dashboard_templates.py`).
"""
import re
import string
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "comfy_test"
_TEMPLATES = _SRC / "reporting" / "report_templates"
_HTML_REPORT = _SRC / "reporting" / "html_report.py"


def _templates():
    return sorted(_TEMPLATES.glob("*.html"))


def test_placeholders_have_kwargs():
    """Every $placeholder must be supplied by html_report.py.

    Rendering uses Template.safe_substitute, which does NOT raise on an unknown
    placeholder -- it emits the literal `$lanes_json` into the page. The result
    is a JS SyntaxError at parse time: blank dashboard, exit code 0, green CI,
    no log line. This is the only thing standing between a one-sided rename and
    a silently broken published site.
    """
    src = _HTML_REPORT.read_text(encoding="utf-8")
    missing = {}
    for tpl in _templates():
        names = {
            m.group("named") or m.group("braced")
            for m in string.Template.pattern.finditer(tpl.read_text(encoding="utf-8"))
            if m.group("named") or m.group("braced")
        }
        absent = sorted(n for n in names if f"{n}=" not in src)
        if absent:
            missing[tpl.name] = absent
    assert not missing, f"placeholders with no kwarg in html_report.py: {missing}"


def test_postmessage_protocol_is_symmetric():
    """Every comfytest-* message sent must be handled by some receiver.

    The frames are published at different times, so a half-finished rename
    (sender updated, listener not) degrades silently: deep links land on the
    wrong lane, the address bar stops tracking, lightbox lane-hopping stops --
    all with no error. Nothing else in the suite can catch that.
    """
    sent, handled = set(), set()
    for tpl in _templates():
        text = tpl.read_text(encoding="utf-8")
        sent |= set(re.findall(r"postMessage\(\s*\{\s*type:\s*'(comfytest-[\w-]+)'", text))
        sent |= set(re.findall(r"pendingMessage\s*=\s*\{\s*type:\s*'(comfytest-[\w-]+)'", text))
        handled |= set(re.findall(r"e\.data\.type\s*===\s*'(comfytest-[\w-]+)'", text))
        handled |= set(re.findall(r"data\.type\s*===\s*'(comfytest-[\w-]+)'", text))

    assert sent, "no comfytest-* messages found -- did the templates move?"
    orphans = sorted(sent - handled)
    assert not orphans, (
        f"message type(s) sent but never handled: {orphans}. "
        f"handled={sorted(handled)}"
    )


def test_no_stale_platform_vocabulary():
    """`platform` must not reappear in the dashboard.

    It means only what sys.platform and wheel tags mean; one lane id component,
    not the lane. See comfy_test/lanes/__init__.py.
    """
    offenders = {
        tpl.name: len(re.findall(r"platform", tpl.read_text(encoding="utf-8"), re.I))
        for tpl in _templates()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"'platform' vocabulary back in templates: {offenders}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
