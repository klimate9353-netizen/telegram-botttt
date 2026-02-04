FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

# Deno (yt_dlp EJS / JS runtime) — deno.land блок бўлса ҳам ишлайди
ARG DENO_VERSION=2.6.8
RUN curl -fsSL --retry 5 --retry-delay 2 \
      https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip \
      -o /tmp/deno.zip \
 && unzip /tmp/deno.zip -d /usr/local/bin \
 && rm /tmp/deno.zip \
 && deno --version

# (ихтиёрий, лекин тавсия) yt_dlp учун default sozlamalar
ENV YTDLP_JS_RUNTIME=deno
ENV YTDLP_REMOTE_EJS=1
ENV YTDLP_SKIP_HLS=1

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
