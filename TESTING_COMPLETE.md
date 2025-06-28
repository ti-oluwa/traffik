# Traffik Library - Complete Testing & Docker Guide

This comprehensive guide covers all testing approaches for the Traffik FastAPI throttling library, including Docker-based cross-platform testing and local development options.

## 🚀 Quick Start

Choose the testing approach that works best for your environment:

### Option 1: Fast Local Testing (No Dependencies)

```bash
git clone https://github.com/ti-oluwa/traffik.git
cd traffik
uv sync --extra test
make test-fast  # Tests without Redis
```

### Option 2: Docker Testing (Full Features)

```bash
git clone https://github.com/ti-oluwa/traffik.git
cd traffik
./docker-test.sh test-fast  # Quick Docker test
./docker-test.sh test       # Full test with Redis
```

### Option 3: Manual Testing

```bash
git clone https://github.com/ti-oluwa/traffik.git
cd traffik
uv sync --extra test
uv run pytest -v -k "not redis"  # Skip Redis tests
```

## 🐳 Docker Testing (Recommended for Full Testing)

### Available Docker Testing Commands

| Command | Description | Redis Required |
|---------|-------------|----------------|
| `./docker-test.sh test-fast` | Quick tests without Redis | No |
| `./docker-test.sh test` | Full test suite | Yes |
| `./docker-test.sh test-matrix` | Test all Python versions | Yes |
| `./docker-test.sh dev` | Development environment | Yes |
| `./docker-test.sh quality` | Code quality checks | No |
| `./docker-test.sh coverage` | Coverage analysis | Yes |
| `./docker-test.sh ci` | Full CI simulation | Yes |

### Docker Compose Files

1. **`docker-compose.yml`** - Production-like testing
2. **`docker-compose.dev.yml`** - Fast development iteration
3. **`docker-compose.matrix.yml`** - Cross-platform testing

### Docker Setup

```bash
# Build images
./docker-test.sh build

# Run full CI pipeline
./docker-test.sh ci

# Development workflow
./docker-test.sh dev
```

See [DOCKER.md](DOCKER.md) for complete Docker documentation.

## 🔧 Local Testing with Makefile

The Makefile provides convenient commands for local development:

### Core Testing Commands

```bash
make test-fast      # Basic tests
make test-inmemory  # In-memory backend only
make test-backends  # All backend tests
make test-coverage  # With coverage analysis
```

### Code Quality Commands

```bash
make lint          # Check code style
make format        # Format code
make quality       # All quality checks
make security      # Security analysis
```

### Development Commands

```bash
make dev-setup     # Set up development environment
make dev           # Quick dev workflow
make example       # Run example code
make debug-env     # Show environment info
```

### All Available Makefile Targets

Run `make help` to see all available commands:

```text
Available targets:
  help            Show this help message
  install         Install the package
  install-dev     Install development dependencies
  install-minimal Install minimal dependencies (no Redis)
  test            Run full test suite (requires Redis)
  test-fast       Run tests without Redis dependency
  test-inmemory   Run only in-memory backend tests
  test-redis      Run only Redis backend tests
  test-backends   Run all backend tests
  test-throttles  Run throttle mechanism tests
  test-decorators Run decorator tests
  test-coverage   Run tests with coverage
  lint            Run linting
  lint-fix        Run linting with auto-fix
  format          Format code
  format-check    Check code formatting
  security        Run security analysis
  type-check      Run type checking
  quality         Run all quality checks
  build           Build the package
  upload-test     Upload to Test PyPI
  upload          Upload to PyPI
  dev-setup       Set up development environment
  clean           Clean up build artifacts and cache
  redis-start     Start Redis using Docker
  redis-stop      Stop Redis container
  ci              Run CI-like checks locally
  debug-env       Show environment information
  example         Run example usage tests
  dev             Quick development workflow
  release-check   Pre-release checks
```

## 🧪 Manual Testing

For manual testing without automation tools:

### Basic Functionality

```bash
# Install and test
uv sync --extra test
uv run python -c "import traffik; print('✅ Import successful')"
```

### Specific Test Categories

```bash
# Backend tests
uv run pytest tests/backends/ -v

# Throttle mechanism tests
uv run pytest tests/test_throttles.py -k "not redis" -v

# Decorator tests
uv run pytest tests/test_decorators.py -v
```

### With Coverage

```bash
uv run pytest --cov=src/traffik --cov-report=html
```

## 🌍 Platform-Specific Testing

### Linux (Full Support)

```bash
# Local testing with all features
make test           # If all backends dependencies installed
make test-fast      # Quick tests without backend specifics

# Docker testing
./docker-test.sh test
./docker-test.sh test-matrix
```

### macOS (Full Support)

```bash
# Same as Linux
make test-fast      # Recommended
./docker-test.sh test  # If Docker Desktop installed
```

### Windows (Core Support)

```bash
# Use cross-platform tests
make test-cross-platform
make test-fast

# PowerShell
uv run pytest -v -k "not redis and not Redis"
```

### WSL (Linux-like on Windows)

```bash
# Full Linux support in WSL
make test-fast
./docker-test.sh test  # If Docker Desktop WSL integration enabled
```

## 🚢 CI/CD Integration

### GitHub Actions

The project includes three workflows:

1. **`test.yaml`** - Cross-platform testing
2. **`code-quality.yaml`** - Code quality checks
3. **`pypi.yaml`** - Package publishing

#### Test Matrix

| Platform | Python Versions | Redis Support |
|----------|----------------|---------------|
| Linux (Ubuntu) | 3.8, 3.9, 3.10, 3.11, 3.12 | ✅ Full support |
| macOS | 3.8, 3.11, 3.12 | ❌ In-memory only |
| Windows | 3.8, 3.11, 3.12 | ❌ In-memory only |

### Local CI Simulation

```bash
# Simulate complete CI pipeline
make ci

# Or with Docker
./docker-test.sh ci
```

## 🔍 Testing Scenarios

### 1. Development Testing

Quick iteration during development:

```bash
make test-fast      # Fastest option
make test-inmemory  # Backend-specific
```

### 2. Feature Testing

Test specific functionality:

```bash
make test-throttles   # Throttling mechanisms
make test-decorators  # Decorator functionality
```

### 3. Integration Testing

Full system testing:

```bash
./docker-test.sh test        # With Redis
make test-coverage           # With coverage
./docker-test.sh test-matrix # Multiple Python versions
```

### 4. Pre-commit Testing

Before committing changes:

```bash
make quality        # Code quality
make test-fast      # Quick functionality test
make ci             # Full CI simulation
```

### 5. Release Testing

Before releasing:

```bash
make release-check        # Pre-release validation
./docker-test.sh ci      # Full Docker CI
make build               # Package building
```

## 🐛 Troubleshooting

### Common Issues

1. **Docker not available**

   ```bash
   # Use local testing
   make test-fast
   ```

2. **Redis connection issues**

   ```bash
   # Skip Redis tests
   make test-fast
   uv run pytest -k "not redis"
   ```

3. **Import errors**

   ```bash
   # Reinstall dependencies
   uv sync --extra test
   make debug-env
   ```

4. **Permission errors**

   ```bash
   chmod +x docker-test.sh
   sudo chown -R $(id -u):$(id -g) .
   ```

### Environment Debugging

```bash
# Check environment
make debug-env

# Manual checks
uv run python --version
uv --version
docker --version  # If available
redis-cli ping    # If Redis installed
```

## 📊 Performance Testing

### Benchmarking

```bash
# Custom performance test
uv run python -c "
import asyncio, time
from traffik.backends.inmemory import InMemoryBackend

async def benchmark():
    backend = InMemoryBackend()
    async with backend:
        start = time.time()
        for i in range(1000):
            await backend.get_wait_period(f'key{i}', 10, 60000)
        print(f'1000 ops: {time.time()-start:.2f}s')

asyncio.run(benchmark())
"
```

## 🔐 Security Testing

```bash
# Security analysis
make security

# Manual security checks
uv run bandit -r src/
uv run safety scan
```

## 📈 Coverage Analysis

```bash
# Generate HTML coverage report
make test-coverage

# View coverage
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

## 🚀 Next Steps

After testing:

1. **Review Results**: Check all test outputs
2. **Fix Issues**: Address any test failures
3. **Quality Check**: Ensure code quality standards
4. **Documentation**: Update docs if needed
5. **Contribute**: Submit pull requests
6. **Release**: Follow release process

## 📚 Additional Resources

- [README.md](README.md) - Main documentation
- [DOCKER.md](DOCKER.md) - Docker-specific guide
- [TESTING.md](TESTING.md) - Quick testing guide
- [CONTRIBUTING.md](CONTRIBUTING.md) - Development guidelines

## 💡 Tips for Contributors

1. **Start with fast tests**: `make test-fast`
2. **Use Docker for full testing**: `./docker-test.sh test`
3. **Check quality early**: `make quality`
4. **Test cross-platform**: Use CI or Docker matrix
5. **Document changes**: Update relevant docs

This comprehensive testing setup ensures the Traffik library works reliably across different platforms, Python versions, and deployment scenarios.
