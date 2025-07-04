# Docker Testing Guide for Traffik

This guide explains how to use Docker to test the traffik library across different platforms and Python versions.

## Quick Start

1. **Build and run full test suite:**

   ```bash
   ./docker-test.sh test
   ```

2. **Run fast tests (no Redis):**

   ```bash
   ./docker-test.sh test-fast
   ```

3. **Start development environment:**

   ```bash
   ./docker-test.sh dev
   ```

## Available Commands

### Testing Commands

- `./docker-test.sh test` - Full test suite with Redis
- `./docker-test.sh test-fast` - Fast tests without Redis
- `./docker-test.sh test-matrix` - Test across all Python versions
- `./docker-test.sh test-py38` - Test on Python 3.8
- `./docker-test.sh test-py312` - Test on Python 3.12
- `./docker-test.sh coverage` - Generate coverage report
- `./docker-test.sh ci` - Run full CI-like test suite

### Development Commands

- `./docker-test.sh dev` - Interactive development environment
- `./docker-test.sh shell` - Open development shell
- `./docker-test.sh watch` - Test watch mode for development
- `./docker-test.sh redis` - Start Redis service only

### Utility Commands

- `./docker-test.sh build` - Build Docker images
- `./docker-test.sh quality` - Run code quality checks
- `./docker-test.sh logs` - Show service logs
- `./docker-test.sh clean` - Clean up Docker resources

## Docker Compose Files

### `docker-compose.yml` (Main)

Complete testing environment with Redis support and multiple Python versions.

**Services:**

- `redis` - Redis 7 Alpine for backend testing
- `test` - Main test suite with Redis
- `test-no-redis` - Tests without Redis dependency
- `test-py38` - Python 3.8 testing
- `test-py312` - Python 3.12 testing
- `dev` - Development environment
- `quality` - Code quality checks
- `coverage` - Coverage analysis

### `docker-compose.dev.yml` (Development)

Optimized for fast development iteration.

**Services:**

- `test-fast` - Quick tests without Redis
- `test-full` - Full tests with Redis
- `redis-dev` - Development Redis instance
- `shell` - Interactive development shell
- `test-watch` - Continuous testing with file watching

### `docker-compose.matrix.yml` (Cross-platform)

Matrix testing across Python versions.

**Services:**

- `test-py38` through `test-py312` - Python version matrix
- `redis-matrix` - Redis for matrix testing

## Usage Examples

### Basic Testing

```bash
# Quick smoke test
./docker-test.sh test-fast

# Full test with Redis
./docker-test.sh test

# Test specific Python version
./docker-test.sh test-py38
```

### Development Workflow

```bash
# Start development environment
./docker-test.sh dev

# In the container, you can:
# - Run tests: uv run pytest
# - Start Python REPL: uv run python
# - Install packages: uv add package-name
# - Run linting: uv run ruff check src/
```

### Continuous Testing

```bash
# Watch mode for TDD
./docker-test.sh watch

# This will:
# - Watch for file changes
# - Automatically re-run tests
# - Show results in real-time
```

### CI/CD Simulation

```bash
# Run complete CI pipeline locally
./docker-test.sh ci

# This runs:
# 1. Code quality checks
# 2. Fast tests
# 3. Full tests with Redis
# 4. Coverage analysis
```

## Manual Docker Commands

If you prefer using Docker Compose directly:

```bash
# Build all images
docker-compose build

# Run specific service
docker-compose up test

# Run tests in background
docker-compose up -d redis
docker-compose run --rm test

# View logs
docker-compose logs -f redis

# Clean up
docker-compose down -v
```

## Platform-Specific Notes

### Windows

- Use PowerShell or WSL2 for best experience
- Ensure Docker Desktop is running
- May need to adjust volume mounting for file watching

### macOS

- Docker Desktop required
- File watching may be slower due to osxfs
- Consider using `:delegated` or `:cached` volume options

### Linux

- Native Docker support
- Best performance for file watching
- Can use Docker without Docker Desktop

## Troubleshooting

### Redis Connection Issues

```bash
# Check Redis status
docker-compose ps redis

# View Redis logs
docker-compose logs redis

# Test Redis connection
docker-compose exec redis redis-cli ping
```

### Build Issues

```bash
# Clean build cache
docker-compose build --no-cache

# Clean all Docker resources
./docker-test.sh clean
```

### Permission Issues

```bash
# Fix permissions (Linux/macOS)
sudo chown -R $(id -u):$(id -g) .

# Or run with user mapping
docker-compose run --user $(id -u):$(id -g) test
```

### Performance Issues

```bash
# Use faster volume mounting (macOS)
docker-compose -f docker-compose.dev.yml up shell

# Reduce build context with .dockerignore
# (already configured in the project)
```

## Environment Variables

- `REDIS_HOST` - Redis hostname (default: redis)
- `REDIS_PORT` - Redis port (default: 6379)
- `PYTHON_VERSION` - Python version for matrix testing
- `COMPOSE_FILE` - Custom compose file path

## Advanced Usage

### Custom Python Version Testing

```bash
# Test with specific Python version
PYTHON_VERSION=3.9 docker-compose -f docker-compose.matrix.yml up test-matrix
```

### Custom Redis Configuration

```bash
# Use external Redis
REDIS_HOST=external-redis.com docker-compose up test
```

### Parallel Testing

```bash
# Run multiple Python versions in parallel
docker-compose -f docker-compose.matrix.yml up \
  test-py38 test-py39 test-py310 test-py311 test-py312
```

## Integration with IDEs

### VS Code

Add this to `.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "./docker-compose run --rm dev python",
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["--tb=short"]
}
```

### PyCharm

1. Configure Docker Compose as Python interpreter
2. Set docker-compose.yml as configuration file
3. Select 'dev' service for development

## Best Practices

1. **Use appropriate compose file:**
   - `docker-compose.yml` for complete testing
   - `docker-compose.dev.yml` for development
   - `docker-compose.matrix.yml` for cross-platform testing

2. **Optimize build times:**
   - Use multi-stage builds
   - Leverage Docker layer caching
   - Keep .dockerignore updated

3. **Resource management:**
   - Clean up regularly with `./docker-test.sh clean`
   - Use `--abort-on-container-exit` for CI
   - Monitor disk usage with `docker system df`

4. **Security:**
   - Don't expose Redis ports in production
   - Use secrets for sensitive configuration
   - Keep base images updated
