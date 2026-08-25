"""Where Comfy Desktop put its active install. Pure, importable, no side effects.

This lived in `cdp_driver.py`, which is not a module but a 4,000-line script:
importing it executes the whole desktop test, including a
`_walk_first_run_wizard` call with a 1200-second timeout and a `sys.exit()`.
`cli/_desktop_runner.py` imported one helper from it while collecting logs at
the end of a run, which meant log collection could block for twenty minutes and
then die on a `SystemExit` that its own `except Exception` could not catch --
losing the logs artifact for the whole lane.

Nothing here touches the network, the filesystem beyond one JSON read, or any
module-level state.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Tuple


def installations_json_path() -> Path:
    """Comfy Desktop's install registry.

    macOS:   ~/Library/Application Support/Comfy Desktop/installations.json
    Windows: %APPDATA%\\Comfy Desktop\\installations.json
    """
    if sys.platform == 'win32':
        appdata = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
        return appdata / 'Comfy Desktop' / 'installations.json'
    return (Path.home() / 'Library' / 'Application Support' /
            'Comfy Desktop' / 'installations.json')


def find_active_comfy_install() -> Tuple[Path, Path, Path, Path]:
    """(install_path, comfy_root, custom_nodes, venv_python) for the active install.

    Raises RuntimeError when Comfy Desktop has not been launched, or has no
    standalone install recorded.
    """
    installations_json = installations_json_path()
    try:
        entries = json.loads(installations_json.read_text())
    except FileNotFoundError:
        raise RuntimeError(
            f'{installations_json} not found (Comfy Desktop not launched?)') from None
    for inst in entries:
        if inst.get('sourceId') == 'standalone' and inst.get('installPath'):
            install_path = Path(inst['installPath'])
            break
    else:
        raise RuntimeError('no standalone install in installations.json')

    comfy_root = install_path / 'ComfyUI'
    custom_nodes = comfy_root / 'custom_nodes'
    venv_bin = 'Scripts' if sys.platform == 'win32' else 'bin'
    venv_exe = 'python.exe' if sys.platform == 'win32' else 'python'
    venv_python = comfy_root / '.venv' / venv_bin / venv_exe
    if not venv_python.exists():
        venv_python = install_path / 'standalone-env' / venv_bin / venv_exe
    return install_path, comfy_root, custom_nodes, venv_python
