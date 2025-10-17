# Multi-stage Dockerfile for testing traffik library
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim as base

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
COPY pyproject.toml uv.lock Makefile ./
COPY src/ ./src/
COPY tests/ ./tests/
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379

# Development stage
FROM base as development
RUN make install-dev
CMD ["bash"]

# Full test suite stage (Python 3.11)
FROM base as test
ARG PYTHON_VERSION=3.11
RUN make install-test
CMD ["make", "test"]

# Fast tests stage (fast tests only)
FROM test as test-fast
CMD ["make", "test-fast"]
