"""Merge all *.log files in a directory into a single chronologically-sorted
system.log. The html report's generate_html_report reads logs/<workflow>.log
to embed under each workflow card; emitting `system.log` makes the merged
stream show under the 'system' card on per-platform desktop pages.

Each input line either starts with an ISO timestamp like
    [2026-05-08 04:09:31.949]
or is a continuation of the previous line (no timestamp at line start).
Continuation lines inherit the previous line's timestamp so they sort
adjacent to it.

Lines are tagged with a 3-letter source prefix derived from the filename
stem (main.log -> MAI, comfyui.log -> SRV, comfyui_8000.log -> UI ) so the
merged stream is readable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]')

SRC_TAG = {
    'main': 'MAI',
    'comfyui': 'SRV',
    'comfyui_8000': 'UI ',
}


def _normalize_stem(name: str) -> str:
    # main.log_2026-05-08T04-06-22-280Z.log → main
    # comfyui.log → comfyui
    # comfyui_8000.prev.log → comfyui_8000
    s = name
    if s.endswith('.log'):
        s = s[:-4]
    s = re.sub(r'\.log_\d{4}-.*$', '', s)
    s = re.sub(r'\.prev\d*$', '', s)
    return s


def merge(logs_dir: Path) -> Path | None:
    if not logs_dir.exists():
        return None
    events: list[tuple[str, str, str]] = []
    last_ts = '0000-00-00 00:00:00.000'
    for f in sorted(logs_dir.iterdir()):
        if not f.is_file() or f.suffix != '.log':
            continue
        if f.name == 'system.log':
            continue
        stem = _normalize_stem(f.name)
        tag = SRC_TAG.get(stem, stem[:3].upper().ljust(3))
        try:
            text = f.read_text(errors='replace')
        except Exception:
            continue
        for line in text.splitlines():
            m = TS_RE.match(line)
            if m:
                last_ts = m.group(1)
            events.append((last_ts, tag, line))
    if not events:
        return None
    events.sort(key=lambda e: e[0])
    out = logs_dir / 'system.log'
    out.write_text(
        '\n'.join(f'[{ts}] [{tag}] {line}' for ts, tag, line in events),
        encoding='utf-8',
    )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print('usage: merge_logs.py <logs_dir>', file=sys.stderr)
        return 2
    out = merge(Path(argv[1]))
    if out is None:
        print('no log files merged', file=sys.stderr)
        return 0
    print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
