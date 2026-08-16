# syntax=docker/dockerfile:1

# ---- Stage 1: build wheels so the runtime image carries no compiler toolchain --------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- Stage 2: runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg: required by yt-dlp to merge separate video/audio streams into clean .mp4.
# The rest: shared libraries the CloakBrowser stealth Chromium links against, plus fonts
# so rendered pages (and the Cloudflare challenge) behave like a real desktop browser.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      ca-certificates \
      fonts-liberation \
      fonts-noto-color-emoji \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libatspi2.0-0 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libglib2.0-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      xdg-utils \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ISL_HOST=0.0.0.0 \
    ISL_PORT=6767 \
    ISL_DOWNLOAD_DIR=/data/downloads \
    ISL_PROFILE_DIR=/data/browser-profile \
    ISL_HEADLESS=1 \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# Bake the stealth Chromium into the image so container start-up does not download ~150 MB.
RUN python -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"

COPY main.py .

# Non-root runtime user. /data holds both the video cache and the browser profile (the
# profile keeps the Cloudflare clearance cookie across restarts).
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data/downloads /data/browser-profile \
 && chown -R appuser:appuser /app /data /opt/cloakbrowser
USER appuser

VOLUME ["/data"]
EXPOSE 6767

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:6767/healthz', timeout=4).status==200 else 1)"

# Single worker on purpose: each worker process would launch its own browser, and the free
# CloakBrowser tier permits one concurrent session. Scale out with replicas + a license key.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "6767", "--workers", "1", "--proxy-headers"]
