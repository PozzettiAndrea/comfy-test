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
    libatspi2.0-0 libxfixes3 && \
    rm -rf /var/lib/apt/lists/*

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Python 3.10 via uv (matches the Windows image)
RUN uv python install 3.10

# Runtime env
ENV COMFY_TEST_WORKSPACE_DIR=/workspaces \
    COMFY_TEST_LOGS_DIR=/logs \
    COMFY_TEST_GPU=1

# Trust bind-mounted repos (different ownership in container vs host)
RUN git config --global --add safe.directory '*'

RUN mkdir -p /workspaces /logs

# Install comfy-test from PyPI at container start (same pattern as Windows image)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
