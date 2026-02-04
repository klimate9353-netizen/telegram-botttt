FROM python:3.13-slim

WORKDIR /app

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Ensure UTF-8 default to avoid any encoding surprises
ENV PYTHONUTF8=1
ENV LANG=C.UTF-8

# Deno (yt-dlp EJS / JS runtime)
ARG DENO_VERSION=2.6.8
RUN curl -fsSL --retry 5 --retry-delay 2 \
    https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip \
    -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

# yt-dlp defaults (can still be overridden by env vars)
ENV YTDLP_JS_RUNTIME=deno
ENV YTDLP_REMOTE_EJS=1
ENV YTDLP_SKIP_HLS=1

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
