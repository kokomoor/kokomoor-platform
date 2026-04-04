# Kokomoor Platform — Job Agent Pipeline
# Multi-stage build for smaller production images.

FROM python:3.11-slim AS base

# Install system dependencies for Playwright.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" \
    && playwright install chromium --with-deps

# Copy platform source.
COPY core/ core/
COPY pipelines/ pipelines/
COPY alembic/ alembic/
COPY alembic.ini ./

# Create data directory for SQLite.
RUN mkdir -p /app/data

# Default command: run the job agent pipeline.
# Override in docker-compose or CLI for other entry points.
CMD ["python", "-m", "pipelines.job_agent"]
