# comfy-test Linux GPU image.
# NVIDIA Container Toolkit on the host injects the driver at runtime,
# so no driver matching or spike stage is needed.
#
# comfy-test is NOT baked into the image — it's installed from PyPI
# at container start by entrypoint.sh, so each `comfy-test dockertest`
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

# Non-root user for everything below. Running tests as root inside the
# container papers over permission bugs that real users hit (e.g. the
# `mkdir C:\ce` admin-required bug that went undetected for months because
# CI was effectively privileged on every platform).
RUN useradd -m -u 1000 -s /bin/bash comfytest && \
    mkdir -p /workspaces /logs /node && \
    chown comfytest:comfytest /workspaces /logs /node

# Stage entrypoint with broad read+exec while we're still root.
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

USER comfytest
WORKDIR /home/comfytest

# uv (installs into ~/.local/bin for the current user)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/comfytest/.local/bin:$PATH"

# Python 3.10–3.13 via uv: pre-install all candidates so per-test venv
# creation (uv venv --python 3.X) is fast regardless of which version
# `_random_python_version()` picks for the run.
RUN uv python install 3.10 3.11 3.12 3.13

# Trust bind-mounted repos (different ownership in container vs host).
# Per-user git config now, since we're no longer root.
RUN git config --global --add safe.directory '*'

# Runtime env
ENV COMFY_TEST_WORKSPACE_DIR=/workspaces \
    COMFY_TEST_LOGS_DIR=/logs \
    COMFY_TEST_GPU=1 \
    NVIDIA_DRIVER_CAPABILITIES=all

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
