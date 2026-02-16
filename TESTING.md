# Traffik Testing Guide

This comprehensive guide covers all testing approaches for the library, including Docker-based cross-platform testing and local development options.

## Quick Start

Choose the testing approach that works best for your environment:

### Option 1: Fast Local Testing

```bash
git clone https://github.com/ti-oluwa/traffik.git

cd traffik

uv sync --extra test

make test-fast
```

### Option 2: Docker Testing (Full Features)

```bash
git clone https://github.com/ti-oluwa/traffik.git

cd traffik

./docker-test.sh test-fast  # Quick Docker test

./docker-test.sh test       # Full test suite
```

### Option 3: Manual Testing

```bash
git clone https://github.com/ti-oluwa/traffik.git

cd traffik

uv sync --extra test

uv run pytest -v -k "not backend"  # Skip throttle backend tests
```

## Docker Testing (Recommended for Full Testing)

### Available Docker Testing Commands

| Command | Description | Redis Required |
|---------|-------------|----------------|
| `./docker-test.sh test-fast` | Quick tests | No |
| `./docker-test.sh test` | Full test suite | Yes |
| `./docker-test.sh test-matrix` | Test all Python versions | Yes |
| `./docker-test.sh dev` | Development environment | Yes |
| `./docker-test.sh quality` | Code quality checks | No |
| `./docker-test.sh coverage` | Coverage analysis | Yes |
| `./docker-test.sh ci` | Full CI simulation | Yes |

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

## Local Testing with Makefile

The Makefile provides convenient commands for local development:

### Core Testing Commands

```bash
make test-fast      # Basic tests
make test m=backends  # All test with the `backends` marker
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
  install-test    Install test dependencies
  test            Run full test suite (requires all depencies)
  test-fast       Run tests without Redis dependency
  test-native     Run test without external dependencies
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

## Manual Testing

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
uv run pytest tests/*/test_throttles*.py -k "not concurrency" -v

# Decorator tests
uv run pytest tests/*/test_decorators.py -v
```

### With Coverage

```bash
uv run pytest --cov=src/traffik --cov-report=html
```

## CI/CD Integration

### GitHub Actions

The project includes three workflows:

1. **`test.yaml`** - Testing suite
2. **`code-quality.yaml`** - Code quality checks
3. **`publish.yaml`** - Package publishing

### Local CI Simulation

```bash
# Simulate complete CI pipeline
make ci

# Or with Docker
./docker-test.sh ci
```

## Testing Scenarios

### 1. Development Testing

Quick iteration during development:

```bash
make test-fast
```

### 2. Feature Testing

Test specific functionality:

```bash
make test m=throttle   # Throttling mechanisms
make test m=decorator  # Decorator functionality
```

### 3. Integration Testing

Full system testing:

```bash
./docker-test.sh test
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

## Troubleshooting

### Common Issues

1. **Docker not available**

   ```bash
   # Use local testing
   make test-fast
   ```

2. **Import errors**

   ```bash
   # Reinstall dependencies
   uv sync --extra test
   make debug-env
   ```

3. **Permission errors**

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

## Security Testing

```bash
# Security analysis
make security

# Manual security checks
uv run bandit -r src/
```

## Coverage Analysis

```bash
# Generate HTML coverage report
make test-coverage-html

# View coverage
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

## Next Steps

After testing:

1. **Review Results**: Check all test outputs
2. **Fix Issues**: Address any test failures
3. **Quality Check**: Ensure code quality standards
4. **Documentation**: Update docs if needed
5. **Contribute**: Submit pull requests
6. **Release**: Follow release process

## Additional Resources

- [DOCKER.md](DOCKER.md) - Docker-specific guide
- [CONTRIBUTING.md](CONTRIBUTING.md) - Development guidelines

## Tips for Contributors

1. **Start with fast tests**: `make test-fast`
2. **Use Docker for full testing**: `./docker-test.sh test`
3. **Check quality early**: `make quality`
4. **Test cross-versions**: Use CI or Docker matrix
5. **Document changes**: Update relevant docs
