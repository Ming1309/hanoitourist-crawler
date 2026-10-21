FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright EXPORT_DIR=/app/exports HEADLESS=true
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 crawler \
    && mkdir -p /app/exports \
    && chown crawler:crawler /app/exports \
    && chmod -R a+rX /ms-playwright
USER crawler
CMD ["hanoitourist", "schedule"]
