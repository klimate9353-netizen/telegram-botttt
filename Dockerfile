FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno (yt-dlp EJS учун JS runtime)
ENV NODE_INSTALL=/root/.node
RUN curl -fsSL https://node.land/install.sh | sh
ENV PATH="${NODE_INSTALL}/bin:${PATH}"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
