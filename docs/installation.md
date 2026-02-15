# Installation

Getting Traffik installed takes about thirty seconds. Let's make sure you get the right extras for your setup.

---

## Requirements

Before you install, make sure you have:

- **Python 3.9+** - Traffik uses modern async features and type hints throughout.
- **FastAPI** or **Starlette** - Traffik integrates with Starlette's `HTTPConnection` model, which FastAPI is built on.

That's it. No mandatory external services, no heavyweight dependencies. The in-memory backend works with no additional setup.

---

## Install Options

Traffik ships with optional extras for each storage backend. Install only what you need.

=== "pip"

    ```bash
    # In-memory backend only (great for development)
    pip install traffik

    # With Redis support
    pip install "traffik[redis]"

    # With Memcached support
    pip install "traffik[memcached]"

    # All backends
    pip install "traffik[all]"

    # Development extras (testing tools, linters, etc.)
    pip install "traffik[dev]"
    ```

=== "uv"

    ```bash
    # In-memory backend only
    uv add traffik

    # With Redis support
    uv add "traffik[redis]"

    # With Memcached support
    uv add "traffik[memcached]"

    # All backends
    uv add "traffik[all]"

    # Development extras
    uv add "traffik[dev]"
    ```

=== "poetry"

    ```bash
    # In-memory backend only
    poetry add traffik

    # With Redis support
    poetry add "traffik[redis]"

    # With Memcached support
    poetry add "traffik[memcached]"

    # All backends
    poetry add "traffik[all]"

    # Development extras
    poetry add "traffik[dev]"
    ```

---

## What Each Extra Installs

| Extra | What it adds | When to use it |
|---|---|---|
| *(none)* | Core package + `InMemoryBackend` | Local development, tests, single-process apps |
| `[redis]` | `redis-py`, `pottery` | Multi-instance production deployments |
| `[memcached]` | `aiomcache` | High-throughput environments where you already run Memcached |
| `[all]` | Everything above | When you want to try all backends or run the full test suite |
| `[dev]` | Testing tools, linters, type checkers | Contributing to Traffik |

---

## Choosing a Backend

!!! tip "Start with InMemory, graduate to Redis"
    During local development and in CI, the `InMemoryBackend` is perfect. It requires zero configuration and zero external services. When you're ready to deploy, swap it for `RedisBackend`, the rest of your code stays the same.

    ```python
    # Development
    from traffik.backends.inmemory import InMemoryBackend
    backend = InMemoryBackend(namespace="myapp")

    # Production (just change these two lines)
    from traffik.backends.redis import RedisBackend
    backend = RedisBackend(connection="redis://localhost:6379/0", namespace="myapp")
    ```

!!! warning "InMemory is not shared across processes"
    The `InMemoryBackend` lives in a single Python process. If you run your app with multiple workers (e.g., `uvicorn --workers 4`), each worker has its own independent counter. Use `RedisBackend` or `MemcachedBackend` for any multi-process or multi-host setup.

!!! info "Memcached caveats"
    Memcached doesn't support atomic increment-and-expire in a single operation the way Redis does, so the `MemcachedBackend` uses best-effort distributed locks. This is fine for most workloads, but if you need strong consistency guarantees, Redis is the safer choice.

---

## Verifying the Installation

```python
import traffik

print(traffik.__version__)  # Should print "1.*.*"
```

Or from the command line:

```bash
python -c "import traffik; print(traffik.__version__)"
```

---

## Fully Typed

Traffik is a [PEP 561](https://peps.python.org/pep-0561/) compliant package. It ships with a `py.typed` marker, which means type checkers like `mypy` and `pyright` pick up its type annotations automatically, no stub packages required.

!!! tip "Pyright / Pylance users"
    Traffik's generics (`Throttle[Request]`, `ThrottleBackend[..., Request]`) resolve correctly with strict mode. If you see false positives, make sure you're on the latest version of your type checker.

---

## Next Steps

You're all set. Head over to the [Quick Start](quickstart.md) to write your first throttled endpoint.

--8<-- "includes/abbreviations.md"
