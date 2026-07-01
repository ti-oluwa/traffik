# Makefile for Traffik development and testing

.PHONY: help install install-dev install-test install-docs install-bench test test-fast test-watch test-coverage test-coverage-xml test-coverage-html bench lint lint-fix format format-check security type-check quality build upload upload-test dev-setup clean ci docs debug-env example dev release-check

# Default target
help: ## Show this help message
	@echo "Traffik Development Makefile"
	@echo ""
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Installation targets
install: ## Install the package and all dependencies
	uv sync --extra all --inexact

install-dev: ## Install development dependencies (Includes test and docs dependencies)
	uv sync --group dev --group test --group docs --group benchmark --inexact

install-test: ## Install testing dependencies
	uv sync --group test --inexact

install-docs: ## Install docs dependencies
	uv sync --group docs --inexact

install-bench: ## Install benchmarking dependencies
	uv sync --group benchmark --inexact

# Testing targets
TEST_ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
test: ## Run full test suite. Requires all dependencies.
	@if [ -n "$(TEST_ARGS)" ]; then \
		echo "Running: uv run pytest $(TEST_ARGS)"; \
		uv run pytest $(TEST_ARGS); \
	else \
		echo "Running all tests"; \
		uv run pytest -v --tb=short; \
	fi
%:
	@:

test-fast: ## Run tests not marked as slow]
	SKIP_REDIS_TESTS=true SKIP_MEMCACHED_TESTS=true uv run pytest -m "not slow" -x -v --tb=short

test-watch: ## Run tests in watch mode
	uv run pytest-watch --onpass "echo 'Tests passed'" --onfail "echo 'Tests failed'" -- -v --tb=short 

test-coverage: ## Run tests with coverage
	uv run pytest --cov=src/traffik --cov-branch --cov-report=term-missing --cov-fail-under=80
	@echo "Coverage report generated. Check the terminal output for details."

test-coverage-xml: ## Run tests with coverage and generate XML report
	uv run pytest --cov=src/traffik --cov-branch --cov-report=xml:coverage.xml --cov-fail-under=80
	@echo "Coverage report generated at coverage.xml"

test-coverage-html: ## Run tests with coverage and generate HTML report
	uv run pytest --cov=src/traffik --cov-branch --cov-report=html:coverage_html_report --cov-fail-under=80
	@echo "HTML coverage report generated at coverage_html_report/index.html"

# Benchmarking targets
BENCH_ARGS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
bench: ## Run benchmarks. Use `make bench scenario_name` to run a specific scenario.
	uv run -m benchmarks $(BENCH_ARGS)
%:
	@:

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

# CI simulation
ci: quality test-coverage ## Run CI-like checks locally

# Documentation
docs: install-docs ## Build and serve documentation
	zensical build
	zensical serve -o

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
        print(f'✅ Traffik library imported and configured successfully!'); \
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


