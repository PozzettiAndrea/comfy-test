# comfy-test Windows GPU image — single-stage, minimal.
#
# Mirrors docker/linux-gpu/Dockerfile structure where Windows allows:
#   uv → uv python install 3.10-3.13 → entrypoint installs comfy-test from PyPI.
#
# Windows-specific essentials (irreducible — no analogue on Linux):
#   - NVIDIA driver bundled (must match host driver bit-for-bit; see README)
#   - VC Redist (torch's c10.dll needs vcruntime140.dll; servercore omits)
#   - 7-Zip (ComfyUI portable archives use BCJ2 compression)
#   - LongPathsEnabled (torch headers + transformers __pycache__ exceed MAX_PATH)
#
# Build context expects:
#   - nvidia-driver-<ver>.exe   (staged from \\192.168.1.19\pxe\scripts\installers\)
#   - Git-2.53.0-64-bit.exe     (staged from same)
#   - entrypoint.ps1
#
# We use windows/server (not servercore) because PyAV → mfplat.dll. Servercore
# strips Media Foundation and ComfyUI imports PyAV at startup.

FROM mcr.microsoft.com/windows/server:ltsc2025

SHELL ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "$ErrorActionPreference='Stop';"]

# 1. NVIDIA driver — MUST match host driver version exactly (see README).
ARG NVIDIA_INSTALLER=nvidia-driver-581.57.exe
COPY ${NVIDIA_INSTALLER} C:\\install\\nv.exe
RUN Start-Process -Wait -FilePath C:\install\nv.exe -ArgumentList '-s','-noreboot','-clean','-passive'; \
    Remove-Item C:\install\nv.exe

# 2. Visual C++ 2015-2022 x64 Redistributable (torch's c10.dll → vcruntime140.dll).
ADD https://aka.ms/vs/17/release/vc_redist.x64.exe C:\\install\\vc.exe
RUN Start-Process -Wait -FilePath C:\install\vc.exe -ArgumentList '/install','/quiet','/norestart'; \
    Remove-Item C:\install\vc.exe

# 3. uv (mirror of Linux's `curl -LsSf https://astral.sh/uv/install.sh | sh`).
RUN [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; \
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile 'C:\install\uv.ps1'; \
    & 'C:\install\uv.ps1'; \
    Remove-Item 'C:\install\uv.ps1'

# 4. Python 3.10-3.13 via uv (direct mirror of the Linux Dockerfile line).
ENV PATH="C:\\Users\\ContainerAdministrator\\.local\\bin;C:\\Windows\\system32;C:\\Windows;C:\\Windows\\System32\\WindowsPowerShell\\v1.0"
RUN uv python install 3.10 3.11 3.12 3.13

# 5. 7-Zip — ComfyUI portable archives use BCJ2; py7zr can't decompress.
ADD https://www.7-zip.org/a/7z2501-x64.exe C:\\install\\7z.exe
RUN Start-Process -Wait -FilePath C:\install\7z.exe -ArgumentList '/S'; \
    Remove-Item C:\install\7z.exe

# 6. LongPathsEnabled — ComfyUI portable's torch headers + transformers
# __pycache__ trees easily exceed MAX_PATH; 7z extracts them via long-path API,
# but Python's shutil.copytree fails without this flag during workspace move.
RUN New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force | Out-Null

# 7. Git — comfy-test clones ComfyUI inside the container
# (platforms/windows/platform.py:103). Add ONLY \cmd to PATH; \bin contains
# Git for Windows's MSYS userland (sh.exe, bash.exe, awk.exe, sed.exe, ssh.exe,
# ...) which leaks into PATH and shadows tools that expect Windows-native cmd
# behavior.
ARG GIT_INSTALLER=Git-2.53.0-64-bit.exe
COPY ${GIT_INSTALLER} C:\\install\\git.exe
RUN Start-Process -Wait -FilePath C:\install\git.exe -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES','/NOCANCEL','/SP-'; \
    Remove-Item C:\install\git.exe

# 8. Trust bind-mounted repos (mirror of Linux's `git config safe.directory '*'`).
ENV PATH="C:\\Program Files\\Git\\cmd;C:\\Users\\ContainerAdministrator\\.local\\bin;C:\\Windows\\system32;C:\\Windows;C:\\Windows\\System32\\WindowsPowerShell\\v1.0"
RUN git config --global --add safe.directory '*'

# 9. Workspaces / logs (mirror of Linux's /workspaces and /logs).
ENV COMFY_TEST_WORKSPACE_DIR=C:\\workspaces \
    COMFY_TEST_LOGS_DIR=C:\\logs \
    COMFY_TEST_GPU=1
RUN New-Item -ItemType Directory -Force -Path C:\workspaces, C:\logs | Out-Null

# 10. Entrypoint installs comfy-test from PyPI at container start.
COPY entrypoint.ps1 C:\\tools\\entrypoint.ps1

# Fully-qualified powershell path: ENV PATH above doesn't include
# C:\Windows\System32\WindowsPowerShell\v1.0 by default at ENTRYPOINT eval time.
ENTRYPOINT ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\tools\\entrypoint.ps1"]
CMD ["--help"]
