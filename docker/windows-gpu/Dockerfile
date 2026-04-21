# Stage-2 comfy-test image. Inherits the proven spike image (CUDA works).
#
# Build context expects:
#   - Git-2.53.0-64-bit.exe   (staged from \\192.168.1.19\pxe\scripts\installers\)
#   - comfy-test-src\          (copy of D:\utils\comfy-test without .venv / .git / __pycache__)

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

# Install comfy-test as a uv tool (matches how cds test does it on the host)
COPY comfy-test-src C:\\tools\\comfy-test-src
RUN $env:PATH += ';C:\Users\ContainerAdministrator\.local\bin;C:\Program Files\Git\cmd'; \
    uv tool install --no-cache C:\tools\comfy-test-src

# Runtime env
ENV COMFY_TEST_WORKSPACE_DIR=C:\\workspaces \
    COMFY_TEST_LOGS_DIR=C:\\logs \
    COMFY_TEST_GPU=1

RUN New-Item -ItemType Directory -Force -Path C:\workspaces,C:\logs | Out-Null

# Activate uv tool shims on every entry
ENV PATH="C:\\Program Files\\Python310\\Scripts;C:\\Program Files\\Python310;C:\\Program Files\\Git\\cmd;C:\\Program Files\\Git\\bin;C:\\Users\\ContainerAdministrator\\.local\\bin;C:\\Windows\\system32;C:\\Windows"

ENTRYPOINT ["comfy-test"]
CMD ["--help"]
