FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data/telegram \
    && chown -R appuser:appuser /app /data/telegram

USER appuser

EXPOSE 8080

CMD ["telegram-proxy-api"]
