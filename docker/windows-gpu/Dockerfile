# Stage-2 comfy-test image. Inherits the proven spike image (CUDA works).
#
# Build context expects:
#   - Git-2.53.0-64-bit.exe   (staged from \\192.168.1.19\pxe\scripts\installers\)
#   - entrypoint.ps1          (installs comfy-test from PyPI at container start)
#
# comfy-test is no longer baked into the image — it's installed at container
# startup by entrypoint.ps1, so each `comfy-test dockertest` invocation gets
# the version currently on PyPI without rebuilding the image. Pin to a
# specific release with `-e COMFY_TEST_VERSION=0.2.46` if reproducibility
# matters.

FROM comfy-test-windows-gpu:spike

SHELL ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "$ErrorActionPreference='Stop';"]

# Git (comfy-test uses it to clone ComfyUI and optional custom nodes)
ARG GIT_INSTALLER=Git-2.53.0-64-bit.exe
COPY ${GIT_INSTALLER} C:\\install\\git.exe
RUN Start-Process -Wait -FilePath C:\install\git.exe -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES','/NOCANCEL','/SP-'; \
    Remove-Item C:\install\git.exe; \
    setx /M PATH ($env:PATH + ';C:\Program Files\Git\cmd;C:\Program Files\Git\bin') | Out-Null

# uv (static single-exe download)
RUN [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; \
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile 'C:\install\uv-install.ps1'; \
    & 'C:\install\uv-install.ps1'; \
    Remove-Item 'C:\install\uv-install.ps1'; \
    setx /M PATH ($env:PATH + ';C:\Users\ContainerAdministrator\.local\bin') | Out-Null

# 7-Zip — ComfyUI portable archives use BCJ2 compression that py7zr can't handle.
# Silent install places 7z.exe at C:\Program Files\7-Zip\7z.exe, where
# comfy-test's find_7z_executable() picks it up automatically.
ADD https://www.7-zip.org/a/7z2501-x64.exe C:\\install\\7z.exe
RUN Start-Process -Wait -FilePath C:\install\7z.exe -ArgumentList '/S'; \
    Remove-Item C:\install\7z.exe

# Enable Windows long path support (>260 chars). ComfyUI portable's torch headers + transformers
# __pycache__ trees easily exceed MAX_PATH; 7z extracts them fine via long-path API, but Python's
# shutil.copytree fails without this registry flag when moving the extracted tree into the workspace.
RUN New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force | Out-Null

# Install comfy-test from PyPI at container start so the image stays
# version-agnostic. Optional COMFY_TEST_VERSION env var pins to a specific
# release; default is latest.
COPY entrypoint.ps1 C:\\tools\\entrypoint.ps1

# Runtime env
ENV COMFY_TEST_WORKSPACE_DIR=C:\\workspaces \
    COMFY_TEST_LOGS_DIR=C:\\logs \
    COMFY_TEST_GPU=1

RUN New-Item -ItemType Directory -Force -Path C:\workspaces,C:\logs | Out-Null

# Activate uv tool shims on every entry
ENV PATH="C:\\Program Files\\Python310\\Scripts;C:\\Program Files\\Python310;C:\\Program Files\\Git\\cmd;C:\\Program Files\\Git\\bin;C:\\Users\\ContainerAdministrator\\.local\\bin;C:\\Windows\\system32;C:\\Windows"

# Use a fully-qualified powershell path: the ENV PATH below doesn't include
# C:\Windows\System32\WindowsPowerShell\v1.0, so bare ["powershell", ...] would
# fail at container start with hcs::CreateProcess 0x2 (file not found).
ENTRYPOINT ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\tools\\entrypoint.ps1"]
CMD ["--help"]
