# sport-flex web UI with Google Chrome (the code launches Playwright's "chrome" channel).
# Built from the repo root:  docker compose -f infra/docker-compose.yml build
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Taipei \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata fonts-noto-cjk \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

COPY sportflex ./sportflex
COPY venues ./venues
COPY config ./config

EXPOSE 8765
CMD ["python", "-m", "sportflex", "ui", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
