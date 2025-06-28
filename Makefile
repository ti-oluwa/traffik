# Makefile for Traffik development and testing

.PHONY: help install install-dev test test-fast test-redis lint format type-check clean build upload coverage docs

# Default target
help: ## Show this help message
	@echo "Traffik Development Makefile"
	@echo ""
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Installation targets
install: ## Install the package
	uv add traffik

install-dev: ## Install development dependencies
	uv sync --extra dev

install-minimal: ## Install minimal dependencies (no Redis)
	uv sync --extra test

# Testing targets
test: ## Run full test suite (requires Redis)
	uv run pytest -v --tb=short

test-fast: ## Run tests without Redis dependency
	uv run pytest -v --tb=short -k "not redis and not Redis"

test-inmemory: ## Run only in-memory backend tests
	uv run pytest -v tests/backends/test_inmemory.py tests/test_throttles*.py::test_http_throttle_inmemory -k "not redis"

test-redis: ## Run only Redis backend tests (requires Redis server)
	uv run pytest -v -k "redis or Redis"

test-backends: ## Run all backend tests
	uv run pytest -v tests/backends/

test-throttles: ## Run throttle mechanism tests
	uv run pytest -v tests/test_throttles*.py

test-decorators: ## Run decorator tests
	uv run pytest -v tests/test_decorators.py

test-coverage: ## Run tests with coverage
	uv run pytest --cov=src/traffik --cov-branch --cov-report=html --cov-report=term-missing --cov-fail-under=80

# Code quality targets
lint: ## Run linting
	uv run ruff check src/ tests/

lint-fix: ## Run linting with auto-fix
	uv run ruff check src/ tests/ --fix

format: ## Format code
	uv run ruff format src/ tests/

format-check: ## Check code formatting
	uv run ruff format src/ tests/ --check

security: ## Run security analysis
	uv run bandit -r src/
	uv run safety scan

type-check: ## Run type checking
	uv run mypy src/ --ignore-missing-imports

quality: lint format-check security type-check ## Run all quality checks

# Build and distribution
build: ## Build the package
	uv build

upload-test: ## Upload to Test PyPI
	uv publish --repository-url https://test.pypi.org/legacy/

upload: ## Upload to PyPI
	uv publish

# Development targets
dev-setup: install-dev ## Set up development environment
	@echo "Development environment set up!"
	@echo "Run 'make test-fast' to run tests without Redis"
	@echo "Run 'make test' to run full test suite (requires Redis)"

clean: ## Clean up build artifacts and cache
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Redis setup (for local development)
redis-start: ## Start Redis using Docker (if available)
	@command -v docker >/dev/null 2>&1 && docker run -d -p 6379:6379 --name traffik-redis redis:7-alpine || echo "Docker not available. Please install Redis manually."

redis-stop: ## Stop Redis container
	@command -v docker >/dev/null 2>&1 && docker stop traffik-redis && docker rm traffik-redis || echo "Docker not available"

# CI simulation
ci: quality test-fast test-coverage ## Run CI-like checks locally

# Documentation
docs: ## Generate documentation (placeholder)
	@echo "Documentation generation not implemented yet"

# Platform-specific testing
test-linux: ## Test on Linux-like environment
	@echo "Running Linux-compatible tests..."
	make test-fast

test-cross-platform: ## Test cross-platform compatibility
	@echo "Running cross-platform tests..."
	make test-inmemory
	make test-decorators


# Debugging helpers
debug-env: ## Show environment information
	@echo "Python version: $$(python --version)"
	@echo "UV version: $$(uv --version)"
	@echo "Pytest version: $$(uv run python -c 'import pytest; print(pytest.__version__)')"
	@echo "Redis available: $$(command -v redis-cli >/dev/null 2>&1 && echo 'Yes' || echo 'No')"
	@echo "Docker available: $$(command -v docker >/dev/null 2>&1 && echo 'Yes' || echo 'No')"

# Example usage
example: ## Run example usage tests
	@echo "Running example usage demonstrations..."
	@uv run python -c "\
import asyncio; \
from traffik.backends.inmemory import InMemoryBackend; \
from traffik.throttles import HTTPThrottle; \
async def demo(): \
    backend = InMemoryBackend(); \
    async with backend: \
        throttle = HTTPThrottle(limit=5, seconds=10); \
        print('✅ Traffik library imported and configured successfully!'); \
        print(f'   Backend: {backend.__class__.__name__}'); \
        print(f'   Throttle: {throttle.limit} requests per {throttle.expires_after}ms'); \
asyncio.run(demo())"

# Quick development workflow
dev: install-dev lint test-fast ## Quick development workflow: install, lint, test

# Release workflow
release-check: quality test-fast build ## Pre-release checks
	@echo "✅ Release checks passed!"
	@echo "   Run 'make upload-test' to upload to Test PyPI"
	@echo "   Run 'make upload' to upload to PyPI"
