"""Windows VM test execution via QEMU."""

import fnmatch
import grp
import os
import shlex
import subprocess
import shutil
import tempfile
import time
import base64
import zipfile
from pathlib import Path
from typing import Callable, Optional, List

# Config file for persistent settings
CONFIG_DIR = Path.home() / ".config" / "comfy-test"
CONFIG_FILE = CONFIG_DIR / "config.toml"

# Default paths to search for QCOW2
DEFAULT_QCOW2_PATHS = [
    Path.home() / "windows-runner-qemu" / "output" / "windows-runner-minimal.qcow2",
    Path.home() / "windows-runner-qemu" / "packer" / "output" / "windows-runner-minimal.qcow2",
]
OVMF_PATH = Path("/usr/share/OVMF/OVMF_CODE.fd")

# WinRM credentials (from packer build)
WINRM_USER = "packer"
WINRM_PASS = "packer123!"
WINRM_PORT = 5985

# Global process tracking for cleanup
websockify_process = None


def get_saved_qcow2_path() -> Optional[Path]:
    """Get QCOW2 path from config file."""
    if CONFIG_FILE.exists():
        try:
            import tomli
            config = tomli.loads(CONFIG_FILE.read_text())
            path_str = config.get("vm", {}).get("qcow2_path")
            if path_str:
                path = Path(path_str)
                if path.exists():
                    return path
        except Exception:
            pass
    return None


def save_qcow2_path(path: Path):
    """Save QCOW2 path to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f'[vm]\nqcow2_path = "{path}"\n')


def find_qcow2() -> Optional[Path]:
    """Find the Windows QCOW2 image."""
    # 1. Check saved config first
    saved = get_saved_qcow2_path()
    if saved:
        return saved

    # 2. Check default paths
    for path in DEFAULT_QCOW2_PATHS:
        if path.exists():
            return path

    return None


def prompt_for_qcow2(log: Callable) -> Optional[Path]:
    """Prompt user for QCOW2 path when not found."""
    log("No Windows QCOW2 image found in default locations.")
    log("Searched:")
    for p in DEFAULT_QCOW2_PATHS:
        log(f"  - {p}")
    log("")

    try:
        path_str = input("Where is your Windows QCOW2 image? ").strip()
        if not path_str:
            return None

        path = Path(path_str).expanduser().resolve()
        if path.exists():
            save_qcow2_path(path)
            log(f"Saved to {CONFIG_FILE}")
            return path
        else:
            log(f"File not found: {path}")
            return None
    except (EOFError, KeyboardInterrupt):
        log("")
        return None


def find_gpu_device() -> Optional[str]:
    """Find NVIDIA GPU PCI address for passthrough."""
    try:
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            # Look for NVIDIA VGA controller
            if "NVIDIA" in line and ("VGA" in line or "3D controller" in line):
                # Extract PCI address (e.g., "01:00.0")
                pci_addr = line.split()[0]
                return pci_addr
    except Exception:
        pass
    return None


def ensure_winrm_installed(log: Callable = print):
    """Ensure pywinrm is installed, installing it if necessary."""
    try:
        import winrm
        return True
    except ImportError:
        log("Installing pywinrm...")
        result = subprocess.run(
            ["pip", "install", "pywinrm"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"Failed to install pywinrm: {result.stderr}")
            return False
        return True


def wait_for_winrm(port: int = WINRM_PORT, timeout: int = 300, log: Callable = print) -> bool:
    """Wait for WinRM to become available and functional."""
    import socket

    # Ensure winrm is installed
    if not ensure_winrm_installed(log):
        raise ImportError("Failed to install pywinrm")

    import winrm

    start = time.time()
    port_open = False

    while time.time() - start < timeout:
        # First check if port is open
        if not port_open:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    port_open = True
                    log("  WinRM port open, waiting for service to initialize...")
            except Exception:
                pass

        # Once port is open, try actual WinRM connection
        if port_open:
            try:
                session = winrm.Session(
                    f'http://127.0.0.1:{port}/wsman',
                    auth=(WINRM_USER, WINRM_PASS),
                    transport='ntlm',
                    read_timeout_sec=15,
                    operation_timeout_sec=10,
                )
                result = session.run_cmd('echo ready')
                if result.status_code == 0:
                    return True
            except Exception as e:
                log(f"  WinRM not ready: {type(e).__name__}")

        time.sleep(5)
        elapsed = int(time.time() - start)
        if elapsed % 30 == 0:
            log(f"  Waiting for Windows to boot... ({elapsed}s)")
    return False


def start_websockify(vnc_port: int = 5900, web_port: int = 6080, log: Callable = print) -> bool:
    """Start websockify to proxy VNC over WebSocket with noVNC."""
    global websockify_process

    # Find noVNC web directory
    novnc_paths = [
        Path.home() / "windows-runner-qemu" / "novnc",
        Path("/usr/share/novnc"),
        Path("/usr/share/webapps/novnc"),
    ]
    novnc_dir = None
    for p in novnc_paths:
        if p.exists() and (p / "vnc.html").exists():
            novnc_dir = p
            break

    try:
        if novnc_dir:
            log(f"  noVNC proxy started on port {web_port}")
            websockify_process = subprocess.Popen(
                ["websockify", "--web", str(novnc_dir), str(web_port), f"localhost:{vnc_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            log(f"  websockify started (no noVNC web dir found)")
            websockify_process = subprocess.Popen(
                ["websockify", str(web_port), f"localhost:{vnc_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        time.sleep(1)

        if websockify_process.poll() is not None:
            log("  websockify failed to start")
            return False

        return True
    except FileNotFoundError:
        log("  websockify not installed (apt install websockify)")
        return False


def stop_websockify():
    """Stop websockify process if running."""
    global websockify_process
    if websockify_process and websockify_process.poll() is None:
        websockify_process.terminate()
        try:
            websockify_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            websockify_process.kill()
        websockify_process = None


def _parse_gitignore(base_dir: Path) -> List[str]:
    """Parse .gitignore patterns from directory."""
    patterns = []
    gitignore_file = base_dir / ".gitignore"
    if gitignore_file.exists():
        for line in gitignore_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            patterns.append(line.rstrip('/'))
    return patterns


class WinRMSession:
    """Simple WinRM session wrapper."""

    def __init__(self, host: str = "127.0.0.1", port: int = WINRM_PORT,
                 username: str = WINRM_USER, password: str = WINRM_PASS):
        try:
            import winrm
        except ImportError:
            raise ImportError("pywinrm is required. Install with: pip install pywinrm")

        self.session = winrm.Session(
            f'http://{host}:{port}/wsman',
            auth=(username, password),
            transport='ntlm',
        )

    def run_cmd(self, command: str, args: List[str] = None) -> subprocess.CompletedProcess:
        """Run a command and return result."""
        if args:
            full_cmd = f'{command} {" ".join(args)}'
        else:
            full_cmd = command

        result = self.session.run_cmd(command, args or [])
        return subprocess.CompletedProcess(
            args=full_cmd,
            returncode=result.status_code,
            stdout=result.std_out.decode('utf-8', errors='replace'),
            stderr=result.std_err.decode('utf-8', errors='replace'),
        )

    def run_ps(self, script: str) -> subprocess.CompletedProcess:
        """Run a PowerShell script and return result."""
        result = self.session.run_ps(script)
        return subprocess.CompletedProcess(
            args=script[:50] + "...",
            returncode=result.status_code,
            stdout=result.std_out.decode('utf-8', errors='replace'),
            stderr=result.std_err.decode('utf-8', errors='replace'),
        )

    def copy_file(self, local_path: Path, remote_path: str) -> bool:
        """Copy a file to the VM using base64 encoding."""
        return self.copy_file_with_progress(local_path, remote_path)

    def copy_file_with_progress(self, local_path: Path, remote_path: str, log: Callable = None) -> bool:
        """Copy a file to the VM using base64 encoding with progress."""
        content = local_path.read_bytes()
        b64_content = base64.b64encode(content).decode('ascii')

        # Split into chunks to avoid command line length limits
        chunk_size = 30000
        chunks = [b64_content[i:i+chunk_size] for i in range(0, len(b64_content), chunk_size)]
        total_chunks = len(chunks)

        # Create file with first chunk
        script = f'''
$bytes = [System.Convert]::FromBase64String(@"
{chunks[0]}
"@)
[System.IO.File]::WriteAllBytes("{remote_path}", $bytes)
'''
        result = self.run_ps(script)
        if result.returncode != 0:
            return False

        # Append remaining chunks if any
        for i, chunk in enumerate(chunks[1:], start=2):
            if log and i % 10 == 0:
                pct = int(i / total_chunks * 100)
                log(f"    {pct}% ({i}/{total_chunks} chunks)")
            script = f'''
$existing = [System.IO.File]::ReadAllBytes("{remote_path}")
$new = [System.Convert]::FromBase64String(@"
{chunk}
"@)
$combined = $existing + $new
[System.IO.File]::WriteAllBytes("{remote_path}", $combined)
'''
            result = self.run_ps(script)
            if result.returncode != 0:
                return False

        return True

    def copy_directory(self, local_dir: Path, remote_dir: str, log: Callable = print) -> bool:
        """Copy a directory to the VM by zipping and extracting."""
        # Parse gitignore patterns
        gitignore_patterns = _parse_gitignore(local_dir)
        always_skip = {'.git', '__pycache__', '.venv', 'node_modules', '.comfy-test', '.comfy-test-env', '.comfy-test-logs'}

        # Create a zip file
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            zip_path = Path(f.name)

        try:
            # Zip the directory
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for file_path in local_dir.rglob('*'):
                    if file_path.is_file():
                        # Skip always-ignored directories
                        if any(p in always_skip for p in file_path.parts):
                            continue

                        # Check gitignore patterns
                        rel_path = file_path.relative_to(local_dir)
                        skip = False
                        for pattern in gitignore_patterns:
                            if fnmatch.fnmatch(file_path.name, pattern) or fnmatch.fnmatch(str(rel_path), pattern):
                                skip = True
                                break
                        if skip:
                            continue

                        arcname = rel_path
                        zf.write(file_path, arcname)

            zip_size = zip_path.stat().st_size / (1024 * 1024)
            log(f"  Created zip archive ({zip_size:.1f} MB)")

            # Copy zip to VM
            remote_zip = f"C:\\temp\\transfer.zip"
            self.run_ps(f'New-Item -ItemType Directory -Force -Path "C:\\temp" | Out-Null')

            log(f"  Uploading to VM...")
            if not self.copy_file_with_progress(zip_path, remote_zip, log=log):
                log("  Failed to copy zip to VM")
                return False

            log(f"  Extracting to {remote_dir}...")
            # Extract on VM
            script = f'''
Remove-Item -Recurse -Force "{remote_dir}" -ErrorAction SilentlyContinue
Expand-Archive -Path "{remote_zip}" -DestinationPath "{remote_dir}" -Force
Remove-Item "{remote_zip}"
'''
            result = self.run_ps(script)
            return result.returncode == 0

        finally:
            zip_path.unlink(missing_ok=True)

    def fetch_file(self, remote_path: str, local_path: Path) -> bool:
        """Fetch a file from the VM."""
        script = f'''
if (Test-Path "{remote_path}") {{
    $bytes = [System.IO.File]::ReadAllBytes("{remote_path}")
    [System.Convert]::ToBase64String($bytes)
}} else {{
    Write-Error "File not found"
    exit 1
}}
'''
        result = self.run_ps(script)
        if result.returncode != 0:
            return False

        try:
            content = base64.b64decode(result.stdout.strip())
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)
            return True
        except Exception:
            return False


def run_vm(
    node_dir: Path,
    output_dir: Path,
    config_file: str = "comfy-test.toml",
    qcow2_path: Optional[Path] = None,
    gpu: bool = False,
    memory: str = "8G",
    cpus: int = 4,
    levels: Optional[List[str]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """Run comfy-test on Windows QCOW2 VM.

    Args:
        node_dir: Path to the custom node directory
        output_dir: Where to save results
        config_file: Config file name
        qcow2_path: Path to QCOW2 image (auto-detected if None)
        gpu: Enable GPU passthrough
        memory: VM memory allocation
        cpus: Number of CPUs
        levels: Test levels to run (None = all)
        log_callback: Function to call with log lines

    Returns:
        Exit code (0 = success)
    """
    log = log_callback or print

    # Find QCOW2 image
    if qcow2_path is None:
        qcow2_path = find_qcow2()
        if qcow2_path is None:
            # Prompt user for path
            qcow2_path = prompt_for_qcow2(log)
            if qcow2_path is None:
                log("To build a Windows image: cd ~/windows-runner-qemu && ./build_qcow2.py")
                return 1

    if not qcow2_path.exists():
        log(f"Error: QCOW2 image not found: {qcow2_path}")
        return 1

    log(f"Using Windows image: {qcow2_path}")

    # Check OVMF
    if not OVMF_PATH.exists():
        log(f"Error: OVMF not found at {OVMF_PATH}")
        log("Install with: sudo apt-get install ovmf")
        return 1

    # Verify config exists
    if not (node_dir / config_file).exists():
        log(f"Error: {config_file} not found in {node_dir}")
        return 1

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create temporary overlay to protect original image
    overlay_path = output_dir / "vm-overlay.qcow2"
    if overlay_path.exists():
        overlay_path.unlink()  # Remove stale overlay from previous run

    result = subprocess.run([
        "qemu-img", "create",
        "-f", "qcow2",
        "-b", str(qcow2_path.resolve()),
        "-F", "qcow2",
        str(overlay_path)
    ], capture_output=True)

    if result.returncode != 0:
        log(f"Error creating overlay: {result.stderr.decode()}")
        return 1

    log(f"Created temporary overlay (original image protected)")

    # Build QEMU command (must match packer build settings)
    qemu_cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-machine", "q35",  # Must match packer build
        "-m", memory,
        "-smp", str(cpus),
        "-cpu", "host,migratable=on,hv-time=on,hv-relaxed=on,hv-vapic=on,hv-spinlocks=0x1fff",
        "-bios", str(OVMF_PATH),
        "-drive", f"file={overlay_path},if=virtio,cache=writeback,discard=ignore,format=qcow2",
        "-device", "virtio-net-pci,netdev=net0",
        "-netdev", f"user,id=net0,hostfwd=tcp::{WINRM_PORT}-:5985",
        "-vnc", ":0",  # VNC on port 5900 for debugging
    ]

    # GPU passthrough
    if gpu:
        gpu_addr = find_gpu_device()
        if gpu_addr:
            log(f"GPU passthrough: {gpu_addr}")
            qemu_cmd.extend(["-device", f"vfio-pci,host={gpu_addr}"])
        else:
            log("Warning: No NVIDIA GPU found for passthrough")

    # Kill any existing QEMU/websockify processes
    subprocess.run(["pkill", "-f", "websockify"], capture_output=True)
    subprocess.run(["pkill", "-f", "qemu-system-x86_64.*windows-runner"], capture_output=True)
    time.sleep(1)

    # Start VM
    log("Starting Windows VM...")

    # Check if we need sg kvm for KVM access
    use_sg_kvm = False
    if Path("/dev/kvm").exists():
        try:
            kvm_gid = grp.getgrnam("kvm").gr_gid
            if kvm_gid not in os.getgroups():
                use_sg_kvm = True
        except KeyError:
            pass

    if use_sg_kvm:
        cmd_str = " ".join(shlex.quote(str(c)) for c in qemu_cmd)
        qemu_process = subprocess.Popen(
            ["sg", "kvm", "-c", cmd_str],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        qemu_process = subprocess.Popen(
            qemu_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Give QEMU a moment to start VNC server
    time.sleep(2)

    # Start noVNC proxy
    if start_websockify(log=log):
        log("Browser VNC: http://localhost:6080/vnc.html")
    log("VNC client: vnc://localhost:5900")

    try:
        # Wait for WinRM
        log("Waiting for Windows to boot...")
        if not wait_for_winrm(timeout=300, log=log):
            log("Error: Windows failed to boot (WinRM timeout)")
            return 1

        log("Windows is ready!")

        # Connect via WinRM
        log("Connecting via WinRM...")
        session = WinRMSession()

        # Test connection
        result = session.run_cmd("hostname")
        if result.returncode != 0:
            log(f"Error: WinRM connection failed: {result.stderr}")
            return 1
        log(f"  Connected to: {result.stdout.strip()}")

        # Copy node to VM
        log(f"Copying {node_dir.name} to VM...")
        remote_node_dir = f"C:\\test-node\\{node_dir.name}"
        if not session.copy_directory(node_dir, remote_node_dir, log=log):
            log("Error: Failed to copy node to VM")
            return 1

        # Install comfy-test
        log("Installing comfy-test...")
        result = session.run_ps('pip install comfy-test')
        if result.returncode != 0:
            log(f"Error installing comfy-test: {result.stderr}")
            return 1

        # Run tests
        log("Running tests...")
        test_cmd = f'cd "{remote_node_dir}" && comfy-test run --platform windows'
        if levels:
            test_cmd += f' --level {",".join(levels)}'
        test_cmd += f' -c {config_file}'

        result = session.run_ps(test_cmd)
        log(result.stdout)
        if result.stderr:
            log(result.stderr)

        # Copy results back
        log("Fetching results...")
        results_dir = f"{remote_node_dir}\\.comfy-test"

        # Get list of result files
        list_result = session.run_ps(f'Get-ChildItem -Recurse "{results_dir}" | Select-Object -ExpandProperty FullName')
        if list_result.returncode == 0:
            for remote_file in list_result.stdout.strip().splitlines():
                if remote_file.strip():
                    rel_path = remote_file.replace(results_dir + "\\", "")
                    local_file = output_dir / rel_path.replace("\\", "/")
                    session.fetch_file(remote_file, local_file)

        return result.returncode

    finally:
        # Shutdown VM
        log("Shutting down VM...")
        try:
            session = WinRMSession()
            session.run_ps('Stop-Computer -Force')
            time.sleep(5)
        except Exception:
            pass

        # Force kill if still running
        if qemu_process.poll() is None:
            qemu_process.terminate()
            try:
                qemu_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                qemu_process.kill()

        # Stop websockify
        stop_websockify()

        # Clean up overlay (original image was never modified)
        if overlay_path.exists():
            overlay_path.unlink()
