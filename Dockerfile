FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app
VOLUME ["/app/data"]

CMD ["python", "-m", "margin_guard"]
