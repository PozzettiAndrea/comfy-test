"""Automatic Windows ISO download via Fido.

Microsoft doesn't publish stable direct download URLs for Windows ISOs --
the page at microsoft.com/software-download/windows11 mints fresh,
session-bound URLs that expire after a few hours. Fido is a small
PowerShell script (https://github.com/pbatard/Fido, BSD-3-Clause) that
drives Microsoft's same web flow programmatically and returns a URL we
can stream to disk.

We fetch Fido on demand from its GitHub release into the VM root, cache
the resulting ISO under `<vm_root>/iso/`, and skip re-download if the
file already exists. The script itself is ~200KB; the ISO is ~6.5GB.

If the user prefers to bring their own ISO, they pass `--iso PATH` and
this whole module is bypassed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ._root import _ps


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


def _fido_path(vm_root: Path) -> Path:
    return vm_root / "iso" / "Fido.ps1"


def _ensure_fido(vm_root: Path) -> Path:
    """Download Fido into vm_root/iso/ if not already present. Returns its
    path on success; exits non-zero on failure."""
    target = _fido_path(vm_root)
    if target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[vm iso] fetching Fido v{FIDO_VERSION} -> {target}")
    # Invoke-WebRequest streams to disk; -UseBasicParsing avoids the IE
    # engine dependency that's been gone for years but the cmdlet still
    # warns about.
    r = _ps(
        f"Invoke-WebRequest -Uri '{FIDO_URL}' -OutFile '{target}' "
        f"-UseBasicParsing",
        check=False,
    )
    if r.returncode != 0 or not target.exists():
        print(f"[vm iso] failed to download Fido from {FIDO_URL}",
              file=sys.stderr)
        sys.exit(1)
    return target


def _iso_cache_path(vm_root: Path, release: str, lang: str, arch: str) -> Path:
    """Stable filename so we can detect an existing cached ISO."""
    safe_lang = lang.replace(" ", "_")
    return vm_root / "iso" / f"Win{release}_{safe_lang}_{arch}.iso"


def download_windows_iso(
    vm_root: Path,
    *,
    release: str = DEFAULT_WIN_RELEASE,
    edition: str = DEFAULT_WIN_EDITION,
    lang:    str = DEFAULT_WIN_LANG,
    arch:    str = DEFAULT_WIN_ARCH,
) -> Path:
    """Fetch (or reuse a cached) Windows ISO into `<vm_root>/iso/`. Returns
    the path to the .iso file on success; calls sys.exit(1) on failure."""
    target = _iso_cache_path(vm_root, release, lang, arch)
    if target.exists() and target.stat().st_size > 1_000_000_000:
        print(f"[vm iso] using cached ISO at {target} "
              f"({target.stat().st_size // (1024*1024)} MB)")
        return target

    fido = _ensure_fido(vm_root)
    print(f"[vm iso] querying Microsoft for Windows {release} {edition} "
          f"{lang} {arch} download URL via Fido")
    print(f"[vm iso] (this can take ~30s; Fido walks the same multi-step "
          f"web flow microsoft.com uses)")

    # Fido prints the resolved URL to stdout when invoked with -GetUrl.
    # We then stream it to the cache path with Invoke-WebRequest. Splitting
    # URL-resolution from download means we can show progress better and
    # restart the download if it fails without re-walking the MS flow.
    r = _ps(
        f"& '{fido}' -Win '{release}' -Lang '{lang}' -Arch '{arch}' -GetUrl",
        capture=True, check=False,
    )
    url = (r.stdout or "").strip().splitlines()
    # Fido emits some informational lines on stderr; the URL is the last
    # non-empty stdout line.
    url = next((line for line in reversed(url)
                if line.startswith("http")), "")
    if not url:
        print(f"[vm iso] Fido didn't return a URL. stdout:\n{r.stdout}\n"
              f"stderr:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"[vm iso] downloading ISO -> {target}")
    print(f"[vm iso] (~5-7 GB; this is the long step. URL: {url[:80]}...)")
    target.parent.mkdir(parents=True, exist_ok=True)
    # BITS isn't reliably available on all hosts; Invoke-WebRequest with
    # -OutFile is the lowest-common-denominator and gets reasonable speed
    # with -UseBasicParsing + $ProgressPreference = SilentlyContinue.
    r = _ps(
        f"$ProgressPreference = 'SilentlyContinue'; "
        f"Invoke-WebRequest -Uri '{url}' -OutFile '{target}' "
        f"-UseBasicParsing",
        check=False,
    )
    if r.returncode != 0 or not target.exists():
        print(f"[vm iso] download failed.", file=sys.stderr)
        sys.exit(1)

    size_mb = target.stat().st_size // (1024 * 1024)
    if size_mb < 1000:
        print(f"[vm iso] downloaded file is suspiciously small ({size_mb} MB); "
              f"likely an error page from MS. Removing and aborting.",
              file=sys.stderr)
        target.unlink(missing_ok=True)
        sys.exit(1)

    print(f"[vm iso] done ({size_mb} MB) -> {target}")
    return target


__all__ = [
    "DEFAULT_WIN_ARCH",
    "DEFAULT_WIN_EDITION",
    "DEFAULT_WIN_LANG",
    "DEFAULT_WIN_RELEASE",
    "download_windows_iso",
]
