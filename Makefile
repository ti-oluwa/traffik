# Makefile for Traffik development and testing

.PHONY: help install install-dev test test-fast test-slow test-unit test-integration test-backend test-redis test-no-redis test-native test-fastapi test-websocket test-concurrent test-coverage test-coverage-xml test-coverage-html lint lint-fix format format-check security type-check quality build upload upload-test dev-setup clean redis-start redis-stop ci docs debug-env example dev release-check

# Default target
help: ## Show this help message
	@echo "Traffik Development Makefile"
	@echo ""
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Installation targets
install: ## Install the package and all dependencies
	uv add traffik[all]

install-dev: ## Install development dependencies
	uv sync --extra dev

# Testing targets
test: ## Run full test suite. Use `make test m=marker_name` to filter by markers.
	@if [ -n "$(m)" ]; then \
		echo "Running tests with marker: $(m)"; \
		uv run pytest -m "$(m)" -v --tb=short; \
	else \
		echo "Running all tests"; \
		uv run pytest -v --tb=short; \
	fi

test-fast: ## Run tests not marked as slow
	uv run pytest -m "not slow" -v --tb=short

test-slow: ## Run slow tests
	uv run pytest -m "slow" -v --tb=short

test-coverage: ## Run tests with coverage
	uv run pytest --cov=src/traffik --cov-branch --cov-report=term-missing --cov-fail-under=80
	@echo "Coverage report generated. Check the terminal output for details."

test-coverage-xml: ## Run tests with coverage and generate XML report
	uv run pytest --cov=src/traffik --cov-branch --cov-report=xml:coverage.xml --cov-fail-under=80
	@echo "Coverage report generated at coverage.xml"

test-coverage-html: ## Run tests with coverage and generate HTML report
	uv run pytest --cov=src/traffik --cov-branch --cov-report=html:coverage_html_report --cov-fail-under=80
	@echo "HTML coverage report generated at coverage_html_report/index.html"

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

type-check: ## Run type checking. mypy is optional, so it will not fail if mypy is not installed.
	@if uv run python -c "import mypy" 2>/dev/null; then \
		echo "Running mypy type check..."; \
		uv run mypy src/ --ignore-missing-imports || true; \
	else \
		echo "mypy not installed, skipping type check"; \
		echo "Install mypy for type checking: uv add --dev mypy"; \
	fi

quality: lint format-check security type-check ## Run all quality checks

# Build and distribution
build: ## Build the package
	uv build

upload: ## Upload to PyPI
	uv publish

upload-test: ## Upload to Test PyPI
	uv publish --index testpypi

# Development targets
dev-setup: install-dev ## Set up development environment
	@echo "Development environment set up!"
	@echo "Run 'make test-fast' to run quick tests"
	@echo "Run 'make test' to run full test suite"

clean: ## Clean up build artifacts and cache
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info/
	rm -rf htmlcov/
	rm -rf coverage_html_report/
	rm -f .coverage
	rm -f coverage.xml
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# Redis setup (for local development)
redis-start: ## Start Redis using Docker (if available)
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps -a --format "table {{.Names}}" | grep -q traffik-redis; then \
			echo "Redis container already exists. Starting..."; \
			docker start traffik-redis; \
		else \
			echo "Creating and starting Redis container..."; \
			docker run -d -p 6379:6379 --name traffik-redis redis:7-alpine; \
		fi; \
		echo "Redis is running on port 6379"; \
	else \
		echo "Docker not available. Please install Redis manually."; \
	fi

redis-stop: ## Stop Redis container
	@if command -v docker >/dev/null 2>&1; then \
		if docker ps --format "table {{.Names}}" | grep -q traffik-redis; then \
			echo "Stopping Redis container..."; \
			docker stop traffik-redis; \
		else \
			echo "Redis container is not running"; \
		fi; \
	else \
		echo "Docker not available"; \
	fi

# CI simulation
ci: quality test-coverage ## Run CI-like checks locally

# Documentation
docs: ## Generate documentation (placeholder)
	@echo "Documentation generation not implemented yet"

# Debugging helpers
debug-env: ## Show environment information
	@echo "Python version: $$(python --version 2>/dev/null || echo 'Not available')"
	@echo "UV version: $$(uv --version 2>/dev/null || echo 'Not available')"
	@echo "Pytest version: $$(uv run python -c 'import pytest; print(pytest.__version__)' 2>/dev/null || echo 'Not available')"
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
