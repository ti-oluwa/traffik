# Installation

Getting Traffik installed takes about thirty seconds. Let's make sure you get the right extras for your setup.

---

## Requirements

Before you install, make sure you have:

- **Python 3.9+** (up to 3.14) - Traffik uses modern async features and type hints throughout.
- **FastAPI** or **Starlette** - Traffik integrates with Starlette's `HTTPConnection` model, which FastAPI is built on.

That's it. No mandatory external services, no heavyweight dependencies. The in-memory backend works with no additional setup.

```bash
pip install traffik
```

---

## Install Options

Traffik ships with one extra per Redis/Memcached client library. Install only what you need.

=== "pip"

    ```bash
    # In-memory backend only - no extra dependencies
    pip install traffik

    # Redis, via redis.asyncio (the default, recommended Redis client)
    pip install "traffik[aioredis]"

    # Redis, via coredis
    pip install "traffik[coredis]"

    # Memcached, via aiomcache
    pip install "traffik[aiomcache]"

    # Memcached, via emcache (Rendezvous hashing, Linux/macOS only)
    pip install "traffik[emcache]"

    # Everything
    pip install "traffik[all]"
    ```

=== "uv"

    ```bash
    uv add traffik
    uv add "traffik[aioredis]"
    uv add "traffik[coredis]"
    uv add "traffik[aiomcache]"
    uv add "traffik[emcache]"
    uv add "traffik[all]"
    ```

=== "poetry"

    ```bash
    poetry add traffik
    poetry add "traffik[aioredis]"
    poetry add "traffik[coredis]"
    poetry add "traffik[aiomcache]"
    poetry add "traffik[emcache]"
    poetry add "traffik[all]"
    ```

!!! note "The `[redis]` and `[memcached]` extras still work"
    Older versions only had one extra per storage system. `traffik[redis]` (→ `aioredis`) and `traffik[memcached]` (→ `aiomcache`) are kept as aliases so nothing breaks if you're upgrading, but prefer the client-specific names above for anything new.

---

## What Each Extra Installs

| Extra | What it adds | When to use it |
|---|---|---|
| *(none)* | Core package + `InMemoryBackend` + `MultiProcessInMemoryBackend` | Local development, tests, single-machine deployments |
| `[aioredis]` | `redis` (`redis.asyncio`), `pottery` | Production, single Redis node or cluster. The default choice - see below. |
| `[coredis]` | `coredis` (Python 3.10+) | You need Sentinel support, or you're already using `coredis` elsewhere |
| `[aiomcache]` | `aiomcache` | Memcached, portable (pure Python client) |
| `[emcache]` | `emcache` (Linux/macOS only) | Memcached, multi-node, need the extra throughput |
| `[all]` | Every extra above | Trying things out, or you genuinely need more than one |

`MultiProcessInMemoryBackend` needs no extra install, it's stdlib `multiprocessing` under the hood but it does need the `fork` process start method, so it's Linux/macOS only in practice. See [Backends](core-concepts/backends.md) for the constraints before reaching for it.

---

## Choosing a Backend

!!! tip "Start with InMemory, graduate to Redis"
    During local development and in CI, `InMemoryBackend` is perfect: zero configuration, zero external services. When you deploy, swap it for `RedisBackend` and the rest of your code stays the same.

    ```python
    # While developing
    from traffik.backends.inmemory import InMemoryBackend
    backend = InMemoryBackend(namespace="myapp")

    # For production, just change these two lines
    from traffik.backends.redis.aioredis import RedisBackend
    backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
    ```

!!! warning "InMemory doesn't share state across processes"
    `InMemoryBackend` lives in a single Python process. If you run your app with multiple workers (`uvicorn --workers 4`, gunicorn, ...), each worker counts independently. Your real limit becomes `configured_limit × worker_count`, silently. For a multi-worker, single-machine setup without Redis, look at `MultiProcessInMemoryBackend` instead. For anything multi-host, use `RedisBackend` or `MemcachedBackend`.

!!! info "Memcached caveats"
    Memcached doesn't give you atomic read-check-write in one round trip the way Redis's Lua scripting does, so `MemcachedBackend` leans on `add`/`cas`-based locking to stay correct. Fine for most workloads. If you need the strongest consistency guarantees under heavy contention, Redis is the safer default.

---

## Verifying the Installation

```python
import traffik

print(traffik.__version__)
```

Or from the command line:

```bash
python -c "import traffik; print(traffik.__version__)"
```

---

## Fully Typed

Traffik is a [PEP 561](https://peps.python.org/pep-0561/) compliant package. It ships with a `py.typed` marker, so type checkers like `mypy` and `pyright` pick up its annotations automatically.

!!! tip "Pyright / Pylance users"
    Traffik's generics (`Throttle[Request]`, `ThrottleBackend[..., Request]`) resolve correctly under strict mode. If you see false positives, make sure you're on a recent type checker version first.

---

## Next Steps

You're all set. Head over to the [Quick Start](quickstart.md) to write your first throttled endpoint.
