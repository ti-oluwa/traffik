# Backends

A backend is **where Traffik keeps score**. Every increment, every counter check,
every lock acquisition goes through the backend. Choosing the right one is usually
the first architectural decision you'll make when adding Traffik to a project.

---

## Choosing a backend

| Feature               | InMemory                   | Redis                                    | Memcached                                                         |
|-----------------------|----------------------------|------------------------------------------|-------------------------------------------------------------------|
| Best for              | Dev, tests, single process | Production, distributed                  | Existing Memcached stacks                                         |
| Persistence           | No                         | Optional (`persistent=True`)             | Optional (`persistent=True` & `track_keys=True`(enables resets))  |
| Distributed           | No                         | Yes                                      | Yes                                                               |
| Lock type             | asyncio RLock              | Redis Lua / Redlock                      | Memcached `add` CAS                                               |
| Overhead              | Lowest                     | Low (Lua scripts, pipelining)            | Low                                                               |
| Requires extra dep    | No                         | `redis` + `pottery`                      | `aiomcache`                                                       |
| `reset()` / `clear()` | Full                       | Full (Lua SCAN)                          | Only when `track_keys=True`                                       |

---

## InMemory backend

The `InMemoryBackend` stores everything in process memory using sharded `OrderedDict`
stores. It is not suitable for multi-process or distributed deployments, but it is
perfect for development and testing.

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(
    namespace="myapp",          # Key prefix for all throttle keys
    persistent=False,           # Clear data on context exit (default)
    on_error="throttle",        # "allow" | "throttle" | "raise" | callable
    number_of_shards=3,         # Shard count for concurrent access (default 3)
    cleanup_frequency=10.0,     # Seconds between expired-key sweeps (default 10.0)
    lock_kind="unfair",         # "fair" | "unfair" (default "unfair")
    lock_blocking=True,         # Block when acquiring locks
    lock_ttl=None,              # Lock TTL in seconds (None = no expiry)
    lock_blocking_timeout=None, # Max wait for locks in seconds
)
```

**Characteristics:**

- Lock striping via shards reduces contention significantly, especially when hitting multiple keys simultaneously.
- A background cleanup task (configurable via `cleanup_frequency`) sweeps expired
  keys on a schedule. Set it to `None` to disable background cleanup; Traffik will
  still lazily evict expired entries on reads.
- `lock_kind="fair"` gives strict FIFO ordering across tasks at the cost of slightly
  higher overhead.

**When to use:** Local development, unit tests, or genuinely single-process
deployments where you never need to share state between workers.

!!! tip "Always use InMemory in tests"
    Swap out your production backend for `InMemoryBackend` in your test suite. It
    requires no external services, resets itself cleanly between test runs, and adds
    no I/O latency. Your tests will be fast enough to make you smile.

---

## Redis backend

`RedisBackend` is the go-to choice for production. It uses `redis.asyncio` for all
operations, pre-loads Lua scripts for atomic increment-with-TTL, and supports two
distinct distributed lock implementations.

### From a URL

```python
from traffik.backends.redis import RedisBackend

backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    persistent=False,
    on_error="throttle",
    lock_type="redis",      # "redis" (default) or "redlock"
    lock_blocking=True,
    lock_ttl=None,
    lock_blocking_timeout=None,
)
```

### From a factory function

When you need full control over the connection (connection pools, TLS, password, etc.),
pass an async factory instead of a URL:

```python
import redis.asyncio as aioredis
from traffik.backends.redis import RedisBackend

async def get_redis():
    return await aioredis.from_url(
        "redis://:secretpassword@redis-host:6379/0",
        decode_responses=True,
        max_connections=20,
    )

backend = RedisBackend(
    get_redis,       # async callable, not a string
    namespace="myapp",
)
```

### Lock types

| `lock_type` | Algorithm                           | Best for                              |
|-------------|-------------------------------------|---------------------------------------|
| `"redis"`   | SET NX EX + Lua fencing             | Single Redis instance, lowest latency |
| `"redlock"` | Redlock (via `pottery.AIORedlock`)  | Redis clusters, multiple instances    |

!!! warning "Redlock is slower by design"
    Redlock involves multiple round-trips across several Redis nodes. Unless you
    are running a genuine Redis cluster with multiple masters, stick with
    `lock_type="redis"`. The added latency of Redlock is rarely worth it for a
    single node.

**Characteristics:**

- Atomic `increment_with_ttl` is implemented as a single Lua script, no race
  conditions between increment and expire.
- `multi_get` uses Redis `MGET` (one round-trip for multiple keys).
- `multi_set` uses a Redis pipeline with `MULTI/EXEC` for atomicity.
- `clear()` / `reset()` scan and delete namespace keys via a Lua script to avoid
  blocking the server.

---

## Memcached backend

`MemcachedBackend` uses `aiomcache` for async Memcached operations. It is a solid
choice when your stack already runs Memcached and adding Redis would increase
operational complexity.

```python
from traffik.backends.memcached import MemcachedBackend

# From explicit host/port
backend = MemcachedBackend(
    host="localhost",
    port=11211,
    pool_size=2,
    pool_minsize=1,
    namespace=":memcached:",
    persistent=False,
    on_error="throttle",
    lock_blocking=True,
    lock_ttl=None,
    lock_blocking_timeout=None,
    track_keys=False,  # see below
)

# Or from a URL
backend = MemcachedBackend(
    url="memcached://localhost:11211",
    namespace=":memcached:",
)
```

**Characteristics:**

- Locks are implemented using Memcached's atomic `add` operation (add only succeeds
  if the key does not exist), with a fencing token for ownership verification.
- Locks are instance-bound, not task-reentrant the way Redis locks are.
- Memcached keys are limited to 250 bytes, keep your `namespace` short.

### The `track_keys` limitation

Memcached has no equivalent of Redis `SCAN`, so Traffik cannot natively list all keys
in a namespace. The `clear()` method, called during non-persistent context teardown,
is therefore a **no-op by default**.

Enable `track_keys=True` to have Traffik maintain a side-car key that records every
key it sets:

```python
backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace=":memcached:",
    track_keys=True,  # enables clear() at the cost of extra writes
)
```

!!! warning "`track_keys=True` adds overhead and is not 100% reliable"
    Every `set` call writes an extra key to the tracking list. In high-throughput
    scenarios, this tracking list can become a bottleneck and may miss keys under
    concurrent writes. Only enable it if you genuinely need `clear()` to work on
    the Memcached backend. An alternative: if Memcached is dedicated to Traffik, you
    can override `clear()` in a subclass to call `flush_all()` instead.

---

## Backend lifecycle

Every backend needs to be started before use and closed when you are done. Traffik
provides three patterns; pick the one that fits your framework.

=== "Lifespan context manager (recommended)"

    ```python
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from traffik.backends.redis import RedisBackend

    backend = RedisBackend("redis://localhost:6379", namespace="myapp")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with backend.lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)
    ```

=== "Manual async with"

    ```python
    async with backend(app) as b:
        # backend is initialized and attached to app.state
        # on exit: reset (if not persistent) then close
        ...
    ```

=== "Manual initialize / close"

    ```python
    await backend.initialize()
    # ... use backend ...
    await backend.close()
    ```

### Without ASGI lifespan (scripts, tests, CLI tools)

When you are not running an ASGI application, for example in a standalone script, a CLI tool, or a test that doesn't need a full app, you can use the backend as an async context manager directly without passing an `app`:

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")

async def main():
    async with backend():
        # backend is ready; use throttles here
        pass
```

This initialises the backend on entry and closes it (calling `reset()` if `persistent=False`) on exit. No ASGI app, no lifespan fixture required. This pattern is particularly useful for one-off scripts, data migration tools, or test helpers that need to exercise throttle logic without spinning up a full server.

---

### Persistence

By default, `persistent=False`, Traffik calls `reset()` on the backend when the
context exits. This wipes all throttle counters, which is what you usually want
between test runs and application restarts.

Set `persistent=True` to keep counter state alive across restarts:

```python
backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    persistent=True,  # counters survive restarts
)
```

### The `on_error` parameter

All three backends share the same error-handling knob:

| Value        | Behaviour on backend error                                    |
|--------------|---------------------------------------------------------------|
| `"throttle"` | Treat the request as if it exceeded the limit (safe default)  |
| `"allow"`    | Let the request through (optimistic)                          |
| `"raise"`    | Propagate the exception to your exception handler             |
| `callable`   | Call your function `(connection, exc_info) -> wait_ms`        |

--8<-- "includes/abbreviations.md"
