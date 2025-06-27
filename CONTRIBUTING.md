# Contributing to Traffik

Thank you for your interest in contributing to Traffik! We welcome contributions from everyone, whether you're fixing a bug, adding a new feature, improving documentation, or helping with testing.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Contributing Guidelines](#contributing-guidelines)
- [Pull Request Process](#pull-request-process)
- [Testing](#testing)
- [Code Style](#code-style)
- [Documentation](#documentation)
- [Issue Reporting](#issue-reporting)
- [Community](#community)

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct. We are committed to providing a welcoming and inclusive environment for all contributors.

### Our Pledge

- Be respectful and inclusive in all interactions
- Focus on constructive feedback and collaboration
- Help create a positive environment for learning and growth
- Show empathy towards other community members

## Getting Started

### Prerequisites

- Python 3.8 or higher
- Git
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Redis server (for Redis backend testing)

### Development Setup

1. **Fork and Clone the Repository**

   ```bash
   # Fork the repository on GitHub first, then:
   git clone https://github.com/YOUR_USERNAME/traffik.git
   cd traffik
   ```

2. **Set Up Development Environment**

   We recommend using `uv` for dependency management:

   ```bash
   # Install uv if you haven't already
   curl -LsSf https://astral.sh/uv/install.sh | sh

   # Install development dependencies
   uv sync --extra dev --extra test
   ```

   Or using pip:

   ```bash
   # Create virtual environment
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate

   # Install in development mode with all dependencies
   pip install -e ".[dev,test]"
   ```

3. **Verify Installation**

   ```bash
   # Run tests to ensure everything is working
   uv run pytest
   # or
   pytest

   # Check code formatting
   uv run ruff check src/ tests/
   # or 
   ruff check src/ tests/
   ```

4. **Set Up Redis (Optional)**

   For testing Redis backend functionality:

   ```bash
   # Using Docker
   docker run -d -p 6379:6379 redis:latest

   # Or install Redis locally
   # Ubuntu/Debian: sudo apt-get install redis-server
   # macOS: brew install redis
   # Start Redis: redis-server
   ```

## Contributing Guidelines

### Types of Contributions

We welcome several types of contributions:

- **Bug fixes**: Fix issues in existing functionality
- **New features**: Add new throttling backends, strategies, or utilities
- **Documentation**: Improve docs, examples, or code comments
- **Testing**: Add or improve test coverage
- **Performance**: Optimize existing code for better performance
- **Refactoring**: Improve code structure and maintainability

### Before You Start

1. **Check existing issues** to see if your contribution is already being worked on
2. **Open an issue** to discuss major changes or new features before implementation
3. **Search previous issues and PRs** to avoid duplicating effort

### Contribution Workflow

1. **Create a new branch** from `main` for your changes:

   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/issue-description
   ```

2. **Make your changes** following our coding standards

3. **Add or update tests** for your changes

4. **Update documentation** if needed

5. **Commit your changes** with descriptive commit messages:

   ```bash
   git add .
   git commit -m "feat: add Redis cluster support for high availability"
   ```

6. **Push to your fork**:

   ```bash
   git push origin feature/your-feature-name
   ```

7. **Create a Pull Request** on GitHub

## Pull Request Process

### Before Submitting

- [ ] Code follows the project's style guidelines
- [ ] All tests pass locally
- [ ] New code has appropriate test coverage
- [ ] Documentation has been updated (if applicable)
- [ ] Commit messages are clear and descriptive

### PR Description Template

```markdown
## Description
Brief description of the changes and why they're needed.

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## How Has This Been Tested?
Describe the tests you ran and how to reproduce them.

## Checklist
- [ ] My code follows the style guidelines of this project
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] New and existing unit tests pass locally with my changes
```

### Review Process

1. **Automated Checks**: All PRs must pass CI/CD checks (tests, linting, type checking)
2. **Code Review**: At least one maintainer will review your PR
3. **Discussion**: Address any feedback or questions from reviewers
4. **Approval**: Once approved, maintainers will merge your PR

## Testing

### Running Tests

```bash
# Run all tests
uv run pytest
# or
pytest

# Run with coverage
uv run pytest --cov=traffik --cov-report=term-missing
# or
pytest --cov=traffik --cov-report=term-missing

# Run specific test file
uv run pytest tests/test_throttles.py
# or
pytest tests/test_throttles.py

# Run tests matching a pattern
uv run pytest -k "test_redis"
# or
pytest -k "test_redis"
```

### Test Categories

- **Unit Tests**: Test individual components in isolation
- **Integration Tests**: Test interaction between components
- **Backend Tests**: Test different throttle backends (in-memory, Redis)
- **Concurrency Tests**: Test behavior under concurrent load

### Writing Tests

- Use descriptive test names: `test_http_throttle_allows_requests_within_limit`
- Test both success and failure cases
- Use appropriate fixtures for setup/teardown
- Mock external dependencies when testing in isolation
- Test edge cases and error conditions

Example test structure:

```python
import pytest
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

@pytest.mark.asyncio
async def test_throttle_allows_requests_within_limit():
    """Test that throttle allows requests within the specified limit."""
    backend = InMemoryBackend()
    async with backend:
        throttle = HTTPThrottle(limit=3, seconds=1)
        # Test implementation...
```

## Code Style

### Formatting and Linting

We use [Ruff](https://docs.astral.sh/ruff/) for code formatting and linting:

```bash
# Check for issues
uv run ruff check src/ tests/
# or
ruff check src/ tests/

# Auto-fix issues
uv run ruff check --fix src/ tests/
# or
ruff check --fix src/ tests/

# Format code
uv run ruff format src/ tests/
# or
ruff format src/ tests/
```

### Style Guidelines

- **Line length**: Maximum 88 characters
- **Python version**: Support Python 3.8+
- **Type hints**: Use type hints for all public APIs
- **Docstrings**: Use Google-style docstrings for all public functions and classes
- **Import sorting**: Use isort-compatible import ordering
- **Variable naming**: Use descriptive names, avoid abbreviations

### Example Code Style

```python
import asyncio
import typing
from typing import Optional

from starlette.requests import Request


class HTTPThrottle:
    """
    HTTP request throttle implementation.
    
    This class provides rate limiting capabilities for HTTP requests
    in FastAPI applications.
    
    Args:
        limit: Maximum number of requests allowed in the time window.
        seconds: Time window duration in seconds.
        identifier: Custom function to identify clients.
    """
    
    def __init__(
        self,
        limit: int,
        seconds: int,
        identifier: Optional[typing.Callable[[Request], str]] = None,
    ) -> None:
        self.limit = limit
        self.seconds = seconds
        self.identifier = identifier or self._default_identifier
    
    async def _default_identifier(self, request: Request) -> str:
        """Generate default client identifier from request."""
        client_ip = request.client.host if request.client else "unknown"
        return f"{client_ip}:{request.url.path}"
```

## Documentation

### Types of Documentation

- **Code Comments**: Explain complex logic and algorithms
- **Docstrings**: Document all public APIs with parameters, return values, and examples
- **README**: Keep examples up-to-date and accurate
- **Contributing Guide**: This document
- **API Reference**: Auto-generated from docstrings

### Documentation Guidelines

- Use clear, concise language
- Provide practical examples
- Keep documentation up-to-date with code changes
- Include error handling examples
- Document performance considerations

### Building Documentation Locally

```bash
# Install documentation dependencies (if added in the future)
# pip install -e ".[docs]"

# For now, documentation is primarily in README.md and docstrings
# Use any markdown viewer to preview changes
```

## Issue Reporting

### Before Opening an Issue

1. **Search existing issues** to avoid duplicates
2. **Check the documentation** to ensure it's not a usage question
3. **Test with the latest version** to see if the issue persists

### Bug Reports

Use the following template for bug reports:

```markdown
**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Set up throttle with configuration...
2. Send requests...
3. See error

**Expected behavior**
A clear description of what you expected to happen.

**Environment:**
- OS: [e.g., Ubuntu 20.04]
- Python version: [e.g., 3.9.0]
- Traffik version: [e.g., 0.1.0]
- FastAPI version: [e.g., 0.115.13]
- Redis version (if applicable): [e.g., 7.0]

**Additional context**
Add any other context about the problem here.
```

### Feature Requests

```markdown
**Is your feature request related to a problem?**
A clear description of what the problem is.

**Describe the solution you'd like**
A clear description of what you want to happen.

**Describe alternatives you've considered**
Any alternative solutions or features you've considered.

**Additional context**
Add any other context or screenshots about the feature request here.
```

## Community

### Getting Help

- **GitHub Issues**: For bug reports and feature requests
- **GitHub Discussions**: For questions, ideas, and general discussion
- **Documentation**: Check README.md for usage examples

### Contributing Ideas

Here are some areas where contributions would be especially valuable:

- **New Backends**: Support for other databases (PostgreSQL, MongoDB, etc.)
- **Advanced Strategies**: Sliding window, token bucket algorithms
- **Monitoring**: Integration with metrics systems (Prometheus, etc.)
- **Performance**: Optimization for high-throughput scenarios
- **Documentation**: More examples, tutorials, and use cases
- **Testing**: Stress testing, benchmarking utilities

### Recognition

Contributors will be acknowledged in:

- GitHub contributors list
- Release notes for significant contributions
- Future CONTRIBUTORS.md file

## Release Process

Maintainers follow this process for releases:

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md` with new features and fixes
3. Create release PR for review
4. Tag release and publish to PyPI
5. Update GitHub release with changelog

---

Thank you for contributing to Traffik! Your efforts help make rate limiting in FastAPI applications better for everyone. 🚀

## Questions?

If you have any questions about contributing, please don't hesitate to:

- Open a GitHub Discussion
- Comment on relevant issues
- Reach out to maintainers

We're here to help and appreciate your interest in improving Traffik!
