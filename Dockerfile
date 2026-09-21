FROM python:3.12-slim-bookworm

WORKDIR /app

# ffmpeg is needed to merge separate audio/video streams.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# Safe, predictable Python output in a container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8

# Deno runs yt-dlp-ejs, which is required for reliable YouTube extraction.
ARG DENO_VERSION=2.6.8
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) deno_arch=x86_64 ;; \
        arm64) deno_arch=aarch64 ;; \
        *) echo "Unsupported Docker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL --retry 5 --retry-delay 2 \
    "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}-unknown-linux-gnu.zip" \
    -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

# Keep dependency installation in a separate cached Docker layer.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# Local PO Token provider used by the bgutil yt-dlp plugin.  It is bound to
# 127.0.0.1 at runtime and is never exposed outside this container.
ARG BGUTIL_VERSION=2.0.0
RUN git clone --depth 1 --branch "${BGUTIL_VERSION}" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && deno install --allow-scripts=npm:canvas --frozen

COPY . .
COPY start.sh /usr/local/bin/start

# Do not run a downloader bot as root. The application may write only under /app.
RUN addgroup --system bot \
    && adduser --system --ingroup bot bot \
    && mkdir -p /app/downloads \
    && chmod 755 /usr/local/bin/start \
    && chown -R bot:bot /app /opt/bgutil
USER bot

ENV BGUTIL_POT_BASE_URL=http://127.0.0.1:4416 \
    DENO_DIR=/tmp/deno-cache

CMD ["/usr/local/bin/start"]
