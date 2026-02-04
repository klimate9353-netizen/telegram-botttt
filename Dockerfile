FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# System deps: ffmpeg for merging audio/video; ca-certificates for HTTPS
RUN apt-get update     && apt-get install -y --no-install-recommends ffmpeg ca-certificates     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

CMD ["python", "main.py"]
