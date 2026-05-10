"""Automatic Windows ISO download.

Two-tier strategy:

1. **Fido** (https://github.com/pbatard/Fido, BSD-3-Clause): drives
   microsoft.com's session-based download flow programmatically and
   returns a fresh URL. Works most of the time but Microsoft aggressively
   rate-limits and outright blocks ranges/regions ("Error: We are unable
   to complete your request at this time. Some users, entities and
   locations are banned from using this service...").

2. **archive.org fallback**: pinned URLs to community-uploaded copies of
   the official Microsoft ISOs (multi-edition, no modifications). Bypass
   the MS rate limit entirely. Slower bandwidth than MS CDN but works
   from anywhere.

Cached under `<vm_root>/iso/Win<release>_<arch>.iso` so re-runs reuse.
The cache key intentionally drops the language -- if the user already
has Win11_x64.iso, we don't re-download a different-language variant
unless they delete the cache first. Pass `--iso PATH` to bypass
auto-download entirely.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from ._root import _ps


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _stream_download(url: str, dest: Path, prefix: str = "[vm iso]") -> bool:
    """Stream `url` -> `dest` with a single-line refreshing progress bar.

    Atomic via .part suffix + replace. Returns True on success, False on
    HTTP/network/IO error (caller decides whether to retry / fail).
    Mirrors the docker/build.py _download() logic; deliberately not
    imported because the prefix is hardcoded there and we want
    '[vm iso]' in the user-visible log."""
    print(f"{prefix} streaming {url}")
    print(f"{prefix}   -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()
    is_tty = sys.stderr.isatty()
    last_print = 0.0
    last_logged = 0  # for non-tty fallback

    try:
        # 5.4GB downloads can stall mid-stream; a long socket timeout keeps
        # us from giving up on slow archive.org mirrors but still aborts
        # if the server stops sending entirely.
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            chunk_size = 1024 * 1024
            written = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_print >= 0.25:  # throttle to ~4Hz
                    last_print = now
                    elapsed = now - t0
                    rate = written / elapsed if elapsed > 0 else 0
                    if total > 0:
                        pct = written * 100 / total
                        bar_w = 30
                        filled = int(bar_w * written / total)
                        bar = "=" * filled + ">" + " " * max(0, bar_w - filled - 1)
                        eta = (total - written) / rate if rate > 0 else 0
                        line = (f"  [{bar}] {_human_size(written)}/"
                                f"{_human_size(total)} ({pct:5.1f}%) "
                                f"{_human_size(rate)}/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s")
                    else:
                        line = (f"  {_human_size(written)} downloaded "
                                f"({_human_size(rate)}/s)")
                    if is_tty:
                        sys.stderr.write("\r" + line + "\033[K")
                        sys.stderr.flush()
                    else:
                        # Non-tty (CI logs etc.): print every ~100MB.
                        if written - last_logged > 100 * 1024 * 1024:
                            last_logged = written
                            print(line, file=sys.stderr)
            if is_tty:
                sys.stderr.write("\n")
        tmp.replace(dest)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"\n{prefix}   download failed: {e}", file=sys.stderr)
        try: tmp.unlink()
        except OSError: pass
        return False

    sz = dest.stat().st_size
    elapsed = time.time() - t0
    rate = sz / elapsed if elapsed > 0 else 0
    print(f"{prefix} done: {_human_size(sz)} in {elapsed:.1f}s "
          f"({_human_size(rate)}/s)")
    return True


# Pinned Fido release. Bump when MS changes their backend and a newer
# Fido handles it (the older version will start returning errors).
FIDO_VERSION = "1.62"
FIDO_URL = (
    f"https://github.com/pbatard/Fido/raw/refs/tags/v{FIDO_VERSION}/Fido.ps1"
)

DEFAULT_WIN_RELEASE = "11"
DEFAULT_WIN_EDITION = "Pro"        # Fido groups Pro/Home/Edu under one ISO
DEFAULT_WIN_LANG    = "English International"
DEFAULT_WIN_ARCH    = "x64"


# archive.org community uploads of unmodified Microsoft ISOs. Used when
# Fido fails (rate-limit, region block, etc.). Tried in order; first one
# that responds 2xx/3xx wins.
#
# Verify a candidate before adding:
#   * archive.org item exists at https://archive.org/details/<item-id>
#   * description states "downloaded from microsoft.com" or similar
#   * SHA256 in the item's text file matches Microsoft's published hash
#     for that build (compare against techbench / hashes.adamx.net)
#   * uploader has a track record of unmodified MS uploads
ARCHIVE_ORG_FALLBACKS = {
    ("11", "x64"): [
        # Win11 24H2 multi-edition (includes Pro), uploaded Oct 2025
        "https://archive.org/download/win11_24h2_english_x64_202510/"
        "Win11_24H2_English_x64.iso",
        # Win11 24H2 multi-edition, uploaded Oct 2024 (older but stable)
        "https://archive.org/download/Win11_24H2_English_x64/"
        "Win11_24H2_English_x64.iso",
    ],
}


def _fido_path(vm_root: Path) -> Path:
    return vm_root / "iso" / "Fido.ps1"


def _ensure_fido(vm_root: Path) -> Path:
    """Download Fido into vm_root/iso/ if not already present. Returns its
    path on success; exits non-zero on failure."""
    target = _fido_path(vm_root)
    if target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    # Fido is ~200KB; no progress bar needed but reuse the same helper for
    # consistency and atomic .part replace.
    if not _stream_download(FIDO_URL, target, prefix="[vm iso] fido:"):
        print(f"[vm iso] failed to download Fido from {FIDO_URL}",
              file=sys.stderr)
        sys.exit(1)
    return target


def _iso_cache_path(vm_root: Path, release: str, arch: str) -> Path:
    """Stable filename so we can detect an existing cached ISO. Language is
    intentionally NOT in the path -- if we already have Win11_x64.iso we
    don't want to download a different-language variant unless the user
    explicitly removes the cache."""
    return vm_root / "iso" / f"Win{release}_{arch}.iso"


def _resolve_url_via_fido(
    vm_root: Path, release: str, lang: str, arch: str,
) -> Optional[str]:
    """Try Fido. Returns a URL on success, None if Fido couldn't get one
    (rate-limited, blocked, etc.). Errors print to stderr but don't exit
    -- the caller falls back to archive.org."""
    fido = _ensure_fido(vm_root)
    print(f"[vm iso] querying Microsoft for Windows {release} {lang} {arch} "
          f"download URL via Fido")
    print(f"[vm iso] (this can take ~30s; Fido walks the same multi-step "
          f"web flow microsoft.com uses)")

    r = _ps(
        f"& '{fido}' -Win '{release}' -Lang '{lang}' -Arch '{arch}' -GetUrl",
        capture=True, check=False,
    )
    lines = (r.stdout or "").strip().splitlines()
    url = next((line for line in reversed(lines)
                if line.startswith("http")), "")
    if url:
        return url

    print(f"[vm iso] Fido couldn't get a URL "
          f"(MS likely rate-limited or blocked this IP).", file=sys.stderr)
    if r.stdout:
        print(f"[vm iso]   Fido stdout: {r.stdout.strip()}", file=sys.stderr)
    return None


def _resolve_url_via_archive_org(release: str, arch: str) -> Optional[str]:
    """Try the pinned archive.org fallbacks for (release, arch). Returns the
    first URL that responds via PowerShell HEAD, or None if all are dead."""
    candidates = ARCHIVE_ORG_FALLBACKS.get((release, arch), [])
    if not candidates:
        return None

    print(f"[vm iso] falling back to archive.org "
          f"({len(candidates)} candidate URL(s))")
    for url in candidates:
        # PS HEAD: Invoke-WebRequest -Method Head returns object on success,
        # throws on >=400. We just check for non-zero exit.
        r = _ps(
            f"try {{ "
            f"  $r = Invoke-WebRequest -Uri '{url}' -Method Head "
            f"      -UseBasicParsing -MaximumRedirection 5 "
            f"      -TimeoutSec 30 -ErrorAction Stop; "
            f"  if ($r.StatusCode -lt 400) {{ 'ok' }} else {{ 'bad' }} "
            f"}} catch {{ 'bad' }}",
            capture=True, check=False,
        )
        if (r.stdout or "").strip() == "ok":
            print(f"[vm iso]   reachable: {url}")
            return url
        print(f"[vm iso]   unreachable: {url}", file=sys.stderr)
    return None


def download_windows_iso(
    vm_root: Path,
    *,
    release: str = DEFAULT_WIN_RELEASE,
    edition: str = DEFAULT_WIN_EDITION,
    lang:    str = DEFAULT_WIN_LANG,
    arch:    str = DEFAULT_WIN_ARCH,
) -> Path:
    """Fetch (or reuse a cached) Windows ISO into `<vm_root>/iso/`.

    Order: cache -> Fido (Microsoft) -> archive.org. Returns the path to
    the .iso on success; sys.exit(1) on failure."""
    target = _iso_cache_path(vm_root, release, arch)
    # Win11 24H2 multi-edition is 5.4 GB. Anything smaller is almost
    # certainly a partial download from a previous failed attempt --
    # ignore it and re-download (the new path uses .part + atomic rename
    # so this can only happen with crashed older versions).
    MIN_VALID_ISO_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB
    if target.exists() and target.stat().st_size > MIN_VALID_ISO_BYTES:
        print(f"[vm iso] using cached ISO at {target} "
              f"({target.stat().st_size // (1024*1024)} MB)")
        return target
    if target.exists():
        sz_mb = target.stat().st_size // (1024 * 1024)
        print(f"[vm iso] cached ISO at {target} is {sz_mb} MB "
              f"(< 4 GB threshold); discarding and re-downloading")
        target.unlink()

    url = (_resolve_url_via_fido(vm_root, release, lang, arch)
           or _resolve_url_via_archive_org(release, arch))
    if not url:
        print(f"[vm iso] no working source for Windows {release} {arch}. "
              f"Pass --iso PATH to a manually downloaded ISO.",
              file=sys.stderr)
        sys.exit(1)

    print(f"[vm iso] downloading ISO (~5-7 GB; this is the long step)")
    if not _stream_download(url, target, prefix="[vm iso]"):
        sys.exit(1)

    size_mb = target.stat().st_size // (1024 * 1024)
    if size_mb < 1000:
        print(f"[vm iso] downloaded file is suspiciously small ({size_mb} MB); "
              f"likely an error page. Removing and aborting.",
              file=sys.stderr)
        target.unlink(missing_ok=True)
        sys.exit(1)
    return target


__all__ = [
    "DEFAULT_WIN_ARCH",
    "DEFAULT_WIN_EDITION",
    "DEFAULT_WIN_LANG",
    "DEFAULT_WIN_RELEASE",
    "download_windows_iso",
]
