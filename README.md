# Traffik

Asynchronous distributed rate limiting for FastAPI/Starlette applications.

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)

## Features

- **Fully Asynchronous**: Built for async/await with non-blocking operations and minimal overhead
- **Distributed-First**: Atomic operations with distributed locks (Redis, Memcached) achieving high accuracy under concurrency
- **10+ Strategies**: Fixed Window, Sliding Window (Log & Counter), Token Bucket, Leaky Bucket, GCRA, and more
- **HTTP & WebSocket**: Full-featured rate limiting for both protocols with per-message throttling support
- **Production-Ready**: Circuit breakers, automatic retries, backend failover, and custom error handling
- **Flexible Integration**: Dependencies, decorators, middleware, or direct calls — use what fits your architecture
- **Highly Extensible**: Simple, well-documented APIs for custom backends, strategies, error handlers, and identifiers
- **Observable**: Rich error context and strategy statistics for monitoring
- **Performance-Optimized**: Lock striping, connection pooling, script caching, and minimal memory footprint

**Built for production workloads.**

## Documentation

Full documentation is available at **[https://ti-oluwa.github.io/traffik/](https://ti-oluwa.github.io/traffik/)**, covering:

- [Getting Started](https://ti-oluwa.github.io/traffik/quickstart/) — up and running in minutes
- [Core Concepts](https://ti-oluwa.github.io/traffik/core-concepts/) — backends, strategies, identifiers, rate formats
- [Integration](https://ti-oluwa.github.io/traffik/integration/) — dependencies, decorators, middleware, direct usage
- [Advanced Features](https://ti-oluwa.github.io/traffik/advanced/) — response headers, throttle rules, quota contexts, and more
- [Configuration](https://ti-oluwa.github.io/traffik/configuration/) — lock settings, environment variables
- [Error Handling](https://ti-oluwa.github.io/traffik/error-handling/) — retries, circuit breakers, fallbacks
- [Extending Traffik](https://ti-oluwa.github.io/traffik/extending/backends/) — custom backends and strategies
- [Testing](https://ti-oluwa.github.io/traffik/testing/) — patterns for testing throttled endpoints
- [Benchmarks](https://ti-oluwa.github.io/traffik/benchmarks/) — performance comparisons

## Installation

```bash
# Basic (InMemory backend only)
pip install traffik

# With Redis support
pip install "traffik[redis]"

# With Memcached support
pip install "traffik[memcached]"

# All backends
pip install "traffik[all]"
```

## Quick Start

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:v1", rate="100/min", backend=backend)

@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": []}
```

See the [Quick Start guide](https://ti-oluwa.github.io/traffik/quickstart/) for more examples including Redis, WebSocket throttling, middleware setup, and advanced patterns.

## License

MIT — see [LICENSE](LICENSE) for details.
