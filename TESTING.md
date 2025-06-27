# Quick Testing Guide

This guide provides multiple ways to test the traffik library across different platforms and environments.

## Prerequisites

- Python 3.8+ installed
- [uv](https://docs.astral.sh/uv/) package manager (recommended) or pip

## Installation & Quick Test

```bash
# Clone the repository
git clone https://github.com/ti-oluwa/traffik.git
cd traffik

# Install dependencies
uv sync --extra test

# Quick smoke test
uv run python -c "import traffik; print('✅ Traffik works!')"
```

## Testing Options

### 1. Using Makefile (Recommended)

The Makefile provides convenient commands for different testing scenarios:

```bash
# Show all available commands
make help

# Run fast tests (no Redis required)
make test-fast

# Run only in-memory backend tests
make test-inmemory

# Run code quality checks
make quality

# Quick development workflow
make dev
```

### 2. Using Docker (Full Testing)

If you have Docker available, use the comprehensive Docker testing setup:

```bash
# Fast tests without Redis
./docker-test.sh test-fast

# Full test suite with Redis
./docker-test.sh test

# Test across Python versions
./docker-test.sh test-matrix

# Development environment
./docker-test.sh dev
```

### 3. Manual Testing

For manual testing without automation tools:

```bash
# Install dependencies
uv sync --extra test

# Run specific test categories
uv run pytest tests/backends/test_inmemory.py -v
uv run pytest tests/test_decorators.py -v
uv run pytest tests/test_throttles.py -k "not redis" -v

# Run with coverage
uv run pytest --cov=src/traffik --cov-report=term-missing
```

## Platform-Specific Testing

### Linux/macOS

```bash
# Full test suite (if Redis is available)
make test

# Without Redis
make test-fast

# Performance tests
make test-performance
```

### Windows

```bash
# Use Windows-compatible commands
uv run pytest -v -k "not redis and not Redis"

# Or use the cross-platform target
make test-cross-platform
```

### CI/CD Environment

```bash
# Simulate CI pipeline locally
make ci

# This runs:
# - Code quality checks
# - Fast tests without Redis
# - Coverage analysis
```

## Testing Different Scenarios

### Without Redis

```bash
# Test only in-memory backend
make test-inmemory

# Test decorators and basic functionality
make test-decorators

# Test throttle mechanisms
uv run pytest tests/test_throttles.py -k "not redis" -v
```

### With Redis (if available)

```bash
# Start Redis (if Docker available)
make redis-start

# Run Redis-specific tests
make test-redis

# Stop Redis
make redis-stop
```

### Concurrent/Stress Testing

```bash
# Run concurrent tests
make test-concurrent

# Or manually
uv run pytest -v -k "concurrent"
```

## Troubleshooting

### Common Issues

1. **Import Errors**

   ```bash
   # Ensure dependencies are installed
   uv sync --extra test
   
   # Check Python path
   uv run python -c "import sys; print(sys.path)"
   ```

2. **Redis Connection Issues**

   ```bash
   # Skip Redis tests
   make test-fast
   
   # Or use specific exclusion
   uv run pytest -k "not redis and not Redis"
   ```

3. **Permission Issues**

   ```bash
   # Make scripts executable
   chmod +x docker-test.sh
   
   # Or use uv directly
   uv run pytest
   ```

### Environment Information

```bash
# Check environment setup
make debug-env

# Manual environment check
uv run python --version
uv --version
```

## Example Usage Verification

Test that the library works correctly:

```bash
# Run example demonstration
make example

# Manual verification
uv run python -c "
import asyncio
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

async def demo():
    backend = InMemoryBackend()
    async with backend:
        throttle = HTTPThrottle(limit=5, seconds=10)
        print('✅ Backend and throttle configured successfully!')

asyncio.run(demo())
"
```

## Performance Testing

```bash
# Run performance tests
make test-performance

# Manual performance test
uv run python -c "
import asyncio
import time
from traffik.backends.inmemory import InMemoryBackend

async def perf_test():
    backend = InMemoryBackend()
    async with backend:
        start = time.time()
        for i in range(1000):
            await backend.get_wait_period(f'test:key:{i}', 10, 60000)
        end = time.time()
        print(f'✅ 1000 operations in {end-start:.2f}s')

asyncio.run(perf_test())
"
```

## Cross-Platform Compatibility

The library is designed to work across platforms:

- **Linux**: Full functionality including Redis
- **macOS**: Full functionality including Redis  
- **Windows**: Core functionality (Redis requires WSL or Docker)

Test cross-platform compatibility:

```bash
# Test core functionality (works everywhere)
make test-cross-platform

# Test platform-specific features
make test-linux  # For Linux-like environments
```

## Continuous Integration

For CI/CD pipelines, use the appropriate workflow based on platform:

```bash
# Linux CI (with Redis)
make ci

# Windows/macOS CI (without Redis)
make test-fast && make quality
```

## Next Steps

After testing locally:

1. **Review Results**: Check test output for any failures
2. **Coverage**: Run `make test-coverage` to see code coverage
3. **Quality**: Run `make quality` to check code quality
4. **Build**: Run `make build` to create distribution packages
5. **Contribute**: See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines

## Getting Help

- Check the [README.md](README.md) for usage examples
- Review [DOCKER.md](DOCKER.md) for Docker-specific testing
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines
- Open an issue for bug reports or feature requests
