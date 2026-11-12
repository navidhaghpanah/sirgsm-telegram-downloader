FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN groupadd --system bot && useradd --system --gid bot --home /app bot \
    && mkdir -p /data/downloads /data/shared_files \
    && chown -R bot:bot /app /data

USER bot
ENV DOWNLOAD_DIR=/data/downloads

CMD ["python", "-m", "app.main"]
