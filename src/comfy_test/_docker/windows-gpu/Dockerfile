# comfy-test Windows GPU image — single-stage, runs as non-admin ContainerUser.
#
# Mirrors docker/linux-gpu/Dockerfile structure where Windows allows:
#   privileged installs as ContainerAdministrator
#   → USER ContainerUser
#   → uv → uv python install 3.10-3.13
#   → entrypoint installs comfy-test from PyPI as ContainerUser.
#
# Running as a true non-admin user (ContainerUser, not in Administrators
# group) catches the class of bug "code accidentally requires admin on
# Windows" before it ships. Matches the windows-cpu / windows-portable-cpu
# stduser wrapper in dispatch-test.yml.
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

# === Stage 1: privileged installs as ContainerAdministrator ===

# 1. NVIDIA driver — MUST match host driver version exactly (see README).
ARG NVIDIA_INSTALLER=nvidia-driver-581.57.exe
COPY ${NVIDIA_INSTALLER} C:\\install\\nv.exe
RUN Start-Process -Wait -FilePath C:\install\nv.exe -ArgumentList '-s','-noreboot','-clean','-passive'; \
    Remove-Item C:\install\nv.exe

# 2. Visual C++ 2015-2022 x64 Redistributable (torch's c10.dll → vcruntime140.dll).
ADD https://aka.ms/vs/17/release/vc_redist.x64.exe C:\\install\\vc.exe
RUN Start-Process -Wait -FilePath C:\install\vc.exe -ArgumentList '/install','/quiet','/norestart'; \
    Remove-Item C:\install\vc.exe

# 3. 7-Zip — ComfyUI portable archives use BCJ2; py7zr can't decompress.
ADD https://www.7-zip.org/a/7z2501-x64.exe C:\\install\\7z.exe
RUN Start-Process -Wait -FilePath C:\install\7z.exe -ArgumentList '/S'; \
    Remove-Item C:\install\7z.exe

# 4. LongPathsEnabled — ComfyUI portable's torch headers + transformers
# __pycache__ trees easily exceed MAX_PATH; 7z extracts them via long-path API,
# but Python's shutil.copytree fails without this flag during workspace move.
# Admin-only HKLM write (intentional, build-time).
RUN New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force | Out-Null

# 5. Git — comfy-test clones ComfyUI inside the container
# (platforms/windows/platform.py:103). Add ONLY \cmd to PATH; \bin contains
# Git for Windows's MSYS userland (sh.exe, bash.exe, awk.exe, sed.exe, ssh.exe,
# ...) which leaks into PATH and shadows tools that expect Windows-native cmd
# behavior.
ARG GIT_INSTALLER=Git-2.53.0-64-bit.exe
COPY ${GIT_INSTALLER} C:\\install\\git.exe
RUN Start-Process -Wait -FilePath C:\install\git.exe -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES','/NOCANCEL','/SP-'; \
    Remove-Item C:\install\git.exe

# 6. Workspace dirs + entrypoint dir + ACLs so ContainerUser can write.
# Bind mounts at runtime project the host's NTFS ACLs into the container;
# the comfy-test docker run code icacls-grants Users on the host source dirs
# so ContainerUser inside the container can write through the mount.
ENV COMFY_TEST_WORKSPACE_DIR=C:\\workspaces \
    COMFY_TEST_LOGS_DIR=C:\\logs \
    COMFY_TEST_GPU=1
RUN New-Item -ItemType Directory -Force -Path C:\workspaces, C:\logs, C:\node, C:\tools | Out-Null; \
    icacls C:\workspaces /grant 'ContainerUser:(OI)(CI)F' /T /Q | Out-Null; \
    icacls C:\logs       /grant 'ContainerUser:(OI)(CI)F' /T /Q | Out-Null; \
    icacls C:\node       /grant 'ContainerUser:(OI)(CI)F' /T /Q | Out-Null; \
    icacls C:\tools      /grant 'ContainerUser:(OI)(CI)RX' /T /Q | Out-Null

# 7. Entrypoint — copy while still admin (ContainerUser only needs read+exec).
COPY entrypoint.ps1 C:\\tools\\entrypoint.ps1

# === Stage 2: switch to non-admin user. Everything below installs into
# ContainerUser's profile or runs as ContainerUser at container start. ===
USER ContainerUser
WORKDIR C:\\Users\\ContainerUser

# 8. uv — same install script as before but executes in ContainerUser's
# profile, so the binary lands in C:\Users\ContainerUser\.local\bin.
RUN [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; \
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile 'C:\Users\ContainerUser\uv.ps1'; \
    & 'C:\Users\ContainerUser\uv.ps1'; \
    Remove-Item 'C:\Users\ContainerUser\uv.ps1'

# 9. PATH for the remaining build steps and container runtime. Git \cmd
# only (see step 5 rationale).
ENV PATH="C:\\Program Files\\Git\\cmd;C:\\Users\\ContainerUser\\.local\\bin;C:\\Windows\\system32;C:\\Windows;C:\\Windows\\System32\\WindowsPowerShell\\v1.0"

# 10. Python 3.10-3.13 via uv (as ContainerUser, lands in profile).
RUN uv python install 3.10 3.11 3.12 3.13

# 11. Trust bind-mounted repos. Writes ContainerUser's .gitconfig.
RUN git config --global --add safe.directory '*'

# Fully-qualified powershell path: ENV PATH above doesn't include
# C:\Windows\System32\WindowsPowerShell\v1.0 by default at ENTRYPOINT eval time.
ENTRYPOINT ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\tools\\entrypoint.ps1"]
CMD ["--help"]
