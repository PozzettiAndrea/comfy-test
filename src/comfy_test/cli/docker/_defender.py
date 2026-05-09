"""Windows Defender exclusion check.

Process-isolated Windows containers store their writable layer at
C:\\ProgramData\\Docker\\windowsfilter\\<id>\\, on the host's NTFS volume.
Defender's WdFilter scans every IO unless the path is excluded. We can't
relocate the writable layer (Moby requires NTFS for windowsfilter), so the
fix is exclusion via the GPO policy registry channel -- which works even
under Tamper Protection.

This module reads both exclusion channels (cmdlet and GPO) and reports
which expected-excluded paths are missing.
"""

import os
import subprocess
import sys
import time
from typing import List


CRITICAL_PATHS = [
    r"C:\ProgramData\Docker",
]


def _read_gpo_exclusions() -> List[str]:
    """Read paths from HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Exclusions\\Paths."""
    if sys.platform != "win32":
        return []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    paths: List[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows Defender\Exclusions\Paths",
        ) as key:
            i = 0
            while True:
                try:
                    name, _value, _kind = winreg.EnumValue(key, i)
                    paths.append(name)
                    i += 1
                except OSError:
                    break
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return paths


def _read_cmdlet_exclusions() -> List[str]:
    """Read (Get-MpPreference).ExclusionPath via PowerShell."""
    if sys.platform != "win32":
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-MpPreference).ExclusionPath -join ';'"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return []
        out = r.stdout.strip()
        return [p for p in out.split(";") if p] if out else []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _path_covered(target: str, exclusions: List[str]) -> bool:
    """Return True if any exclusion entry covers `target` (case-insensitive prefix match)."""
    t = target.rstrip("\\/").lower()
    for e in exclusions:
        if not e:
            continue
        ee = e.rstrip("\\/").lower()
        if t == ee or t.startswith(ee + "\\") or ee.startswith(t + "\\") or t.startswith(ee + "/"):
            return True
    return False


def check_defender_exclusions(critical_paths: List[str] = None) -> List[str]:
    """Return the subset of critical_paths NOT excluded from Defender.

    Empty list => everything excluded, or non-Windows host. Callers should
    treat empty-list as "no warning needed".
    """
    if sys.platform != "win32":
        return []
    paths = critical_paths if critical_paths is not None else CRITICAL_PATHS
    exclusions = _read_gpo_exclusions() + _read_cmdlet_exclusions()
    return [p for p in paths if not _path_covered(p, exclusions)]


def _fix_command_powershell(missing: List[str]) -> str:
    lines = [
        "$reg = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Exclusions\\Paths'",
        "New-Item -Path $reg -Force | Out-Null",
    ]
    for p in missing:
        lines.append(f"New-ItemProperty -Path $reg -Name '{p}' -Value 0 -PropertyType DWord -Force | Out-Null")
    lines.append("gpupdate /force")
    return "\n    ".join(lines)


def _fix_command_cmd(missing: List[str]) -> str:
    parts = []
    for p in missing:
        parts.append(
            f'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Exclusions\\Paths" '
            f'/v "{p}" /t REG_DWORD /d 0 /f'
        )
    parts.append("gpupdate /force")
    return " && ".join(parts)


# Back-compat alias for older callers.
_fix_command = _fix_command_powershell


def print_warning(missing: List[str], pause_seconds: int = 5) -> None:
    """Print a loud warning + the fix command, then sleep `pause_seconds`."""
    bar = "=" * 60
    print()
    print(bar)
    print("WARNING: WINDOWS DEFENDER WILL SCAN DOCKER CONTAINER IO")
    print(bar)
    print("The following paths are not excluded from Defender:")
    for p in missing:
        print(f"  {p}")
    print()
    print("Container writable-layer IO will be scanned on every read/write,")
    print("slowing tests by 30-50% on heavy workloads (HF model downloads,")
    print("pip installs, ComfyUI graph execution).")
    print()
    print("Run this once as Administrator to add the exclusion (survives")
    print("Tamper Protection because it uses the GPO policy channel):")
    print()
    print("  PowerShell:")
    print(f"    {_fix_command_powershell(missing)}")
    print()
    print("  cmd.exe (one-liner):")
    print(f"    {_fix_command_cmd(missing)}")
    print()
    print(f"Continuing in {pause_seconds} seconds. Set --no-defender-warn or")
    print("COMFY_TEST_NO_DEFENDER_WARN=1 to silence.")
    print(bar)
    print()
    if pause_seconds > 0:
        time.sleep(pause_seconds)


def warn_if_needed(args=None, paths: List[str] = None) -> None:
    """Convenience: check + print warning unless silenced. Safe to call on Linux."""
    if sys.platform != "win32":
        return
    if os.environ.get("COMFY_TEST_NO_DEFENDER_WARN", "").lower() in ("1", "true", "yes"):
        return
    if args is not None and getattr(args, "no_defender_warn", False):
        return
    missing = check_defender_exclusions(paths)
    if missing:
        print_warning(missing)
