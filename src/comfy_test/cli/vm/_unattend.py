"""Build the answer ISO that drives the unattended Windows install.

The ISO has two files at its root:
  * autounattend.xml -- Windows Setup auto-discovers this on any attached
    drive and uses it as the answer file (no need to know the drive
    letter or use a specific volume label).
  * post-install.ps1 -- our state-machine script. autounattend.xml's
    FirstLogonCommands copies it to C:\\comfy-setup\\ on first logon and
    runs it.

ISO generation uses IMAPI2FS COM (built into Windows; no Windows ADK or
oscdimg dependency). We shell out to PowerShell which on-the-fly-compiles
a tiny C# helper that drains IStream from CreateResultImage() into a
file -- the standard "New-IsoFile" pattern.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from ._root import _ps


_TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class Params:
    password: str
    runner_url: str
    runner_token: str
    nvidia_driver_url: str
    python_url: str
    comfy_desktop_url: str
    runner_version: str
    win_edition: str = "Windows 11 Pro"
    computer_name: str = "COMFYDESKTOPGPU"
    timezone: str = "UTC"
    # Optional persistent-storage SMB share. If unc is None, the
    # post-install script's mount step is a no-op.
    shared_folder_unc: Optional[str] = None
    shared_folder_user: Optional[str] = None
    shared_folder_password: Optional[str] = None


def _substitute(template: str, mapping: dict) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace(f"<<{k}>>", v)
    return out


def _render_autounattend(p: Params) -> str:
    src = (_TEMPLATE_DIR / "autounattend.xml").read_text(encoding="utf-8")
    return _substitute(src, {
        "WIN_EDITION":   xml_escape(p.win_edition),
        "PASSWORD_XML":  xml_escape(p.password),
        "COMPUTER_NAME": xml_escape(p.computer_name),
        "TIMEZONE":      xml_escape(p.timezone),
    })


def _render_post_install(p: Params) -> str:
    src = (_TEMPLATE_DIR / "post-install.ps1").read_text(encoding="utf-8")
    return _substitute(src, {
        "RUNNER_URL":              p.runner_url,
        "RUNNER_TOKEN":            p.runner_token,
        "NVIDIA_DRIVER_URL":       p.nvidia_driver_url,
        "PYTHON_URL":              p.python_url,
        "COMFY_DESKTOP_URL":       p.comfy_desktop_url,
        "RUNNER_VERSION":          p.runner_version,
        "SHARED_FOLDER_UNC":       p.shared_folder_unc      or "",
        "SHARED_FOLDER_USER":      p.shared_folder_user     or "",
        "SHARED_FOLDER_PASSWORD":  p.shared_folder_password or "",
    })


# Standard "New-IsoFile" — compiles a tiny C# class with /unsafe to drain
# the IMAPI2FS IStream into a file. Reads $Source + $Path from the script
# scope (we set those before this block). Pure Windows PowerShell 5.1.
_ISO_BUILD_SCRIPT = r"""
$cp = New-Object CodeDom.Compiler.CompilerParameters
$cp.CompilerOptions = '/unsafe'
$cp.WarningLevel = 4
$cp.TreatWarningsAsErrors = $true
Add-Type -CompilerParameters $cp -TypeDefinition @"
public class ISOFile {
    public unsafe static void Create(string Path, object Stream, int BlockSize, int TotalBlocks) {
        int bytes = 0;
        byte[] buf = new byte[BlockSize];
        var ptr = (System.IntPtr)(&bytes);
        var o = System.IO.File.OpenWrite(Path);
        var i = Stream as System.Runtime.InteropServices.ComTypes.IStream;
        if (o != null) {
            while (TotalBlocks-- > 0) {
                i.Read(buf, BlockSize, ptr);
                o.Write(buf, 0, bytes);
            }
            o.Flush();
            o.Close();
        }
    }
}
"@

$image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$image.FileSystemsToCreate = 3   # ISO9660 + Joliet
$image.VolumeName = 'ANSWER'
$image.Root.AddTree($Source, $false)  # $false: don't include base dir
$result = $image.CreateResultImage()
[ISOFile]::Create($Path, $result.ImageStream, $result.BlockSize, $result.TotalBlocks)
"""


def build_setup_iso(vm_root: Path, params: Params) -> Path:
    """Render templates and build `vm_root/setup.iso`. Returns the ISO path.

    Caller should attach the ISO as a second DVD drive on the VM and detach
    it before snapshotting (the file contains the runner registration
    token and ci-runner password).
    """
    vm_root.mkdir(parents=True, exist_ok=True)
    out = vm_root / "setup.iso"

    staging = Path(tempfile.mkdtemp(prefix="comfy-setup-iso-"))
    try:
        (staging / "autounattend.xml").write_text(
            _render_autounattend(params), encoding="utf-8"
        )
        # PS scripts are routinely UTF-8; we don't need a BOM and Windows
        # PowerShell 5.1 reads UTF-8 fine when invoked with -File.
        (staging / "post-install.ps1").write_text(
            _render_post_install(params), encoding="utf-8"
        )

        # Set $Source / $Path via single-quoted PS strings (literal, no
        # interpolation; backslashes in Windows paths pass through unmodified).
        # Paths from tempfile.mkdtemp + Hyper-V VM root are not user-supplied
        # so single-quote escaping isn't a concern here.
        prelude = f"$ErrorActionPreference = 'Stop'\n$Source = '{staging}'\n$Path = '{out}'\n"
        _ps(prelude + _ISO_BUILD_SCRIPT)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return out
