# comfy-test Linux GPU image.
# NVIDIA Container Toolkit on the host injects the driver at runtime,
# so no driver matching or spike stage is needed.
#
# comfy-test is NOT baked into the image — it's installed from PyPI
# at container start by entrypoint.sh, so each `comfy-test docker run`
# invocation gets the version currently on PyPI without rebuilding the
# image. Pin to a specific release with `-e COMFY_TEST_VERSION=0.3.5`
# if reproducibility matters.

FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# System deps:
#   - libxcb1, libgl1-mesa-glx, libglib2.0-0: OpenCV (cv2)
#   - libnspr4, libnss3, libatk*, libcups2, libdrm2, libxkbcommon0,
#     libxcomposite1, libxdamage1, libxrandr2, libgbm1, libpango*,
#     libasound2: Playwright/Chromium headless (for screenshot capture)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    libxcb1 libgl1-mesa-glx libglib2.0-0 \
    libnspr4 libnss3 libnss3-tools libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libxfixes3 \
    libegl1 libgl1 libgles2 && \
    rm -rf /var/lib/apt/lists/*

# Full uid isolation: NO baked container user. Runtime uid is whatever
# the host caller passes via `--user $UID:$GID` (set by
# cli/docker/run.py:_run_linux and the build.sh smoke test). Bugs the
# original `comfytest` user was meant to surface (non-root permission
# issues) still surface — the container still runs unprivileged, just
# as the caller's uid instead of a build-time stranger.
#
# Read-only tooling under /opt/uv (chmod a+rX); writable runtime state
# under /tmp (0777). No /home anywhere.

ENV UV_INSTALL_DIR=/opt/uv/bin \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    HOME=/tmp/comfytest-home \
    UV_TOOL_DIR=/tmp/comfytest-tools \
    UV_TOOL_BIN_DIR=/tmp/comfytest-tools/bin \
    PATH=/tmp/comfytest-tools/bin:/opt/uv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# uv binary + Python interpreters into /opt/uv. Pre-installing 3.10-3.13
# so per-test `uv venv --python 3.X` is fast regardless of which version
# _random_python_version() picks.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv python install 3.10 3.11 3.12 3.13 && \
    chmod -R a+rX /opt/uv

# Runtime mountpoints + state dirs — writable by any uid that --user sets.
RUN mkdir -p /workspaces /logs /node /tmp/comfytest-home /tmp/comfytest-tools/bin && \
    chmod 0777 /workspaces /logs /node /tmp/comfytest-home /tmp/comfytest-tools /tmp/comfytest-tools/bin

# System-wide git config — visible to any runtime uid without needing
# a per-user ~/.gitconfig fixture.
RUN git config --system --add safe.directory '*'

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

WORKDIR /tmp/comfytest-home

# No USER directive — caller MUST pass --user. Without it the container
# runs as root, which we want loud (so missed `--user` flags fail closed
# in any sane test). build.sh smoke test and _run_linux both pass --user.

ENV COMFY_TEST_WORKSPACE_DIR=/workspaces \
    COMFY_TEST_LOGS_DIR=/logs \
    COMFY_TEST_GPU=1 \
    NVIDIA_DRIVER_CAPABILITIES=all

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
