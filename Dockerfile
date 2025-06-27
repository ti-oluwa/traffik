# Multi-stage Dockerfile for testing traffik library
FROM python:3.11-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY README.md CONTRIBUTING.md LICENSE ./

# Install dependencies
RUN uv sync --extra test --extra redis --extra dev

# Development stage
FROM base as development
CMD ["bash"]

# Test stage
FROM base as test
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379
CMD ["uv", "run", "pytest", "-v"]

# Test stage without Redis (for platforms that don't support Redis containers)
FROM base as test-no-redis
CMD ["uv", "run", "pytest", "-v", "-k", "not redis and not Redis"]

# Multi-Python testing stages
FROM python:3.8-slim as test-py38
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache

RUN apt-get update && apt-get install -y curl git build-essential && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY README.md CONTRIBUTING.md LICENSE ./

RUN uv sync --extra test --extra redis --extra dev
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379
CMD ["uv", "run", "pytest", "-v"]

FROM python:3.12-slim as test-py312
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache

RUN apt-get update && apt-get install -y curl git build-essential && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY README.md CONTRIBUTING.md LICENSE ./

RUN uv sync --extra test --extra redis --extra dev
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379
CMD ["uv", "run", "pytest", "-v"]
