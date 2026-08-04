# Backends

A backend is **where Traffik keeps score**. Every increment, every counter check, every lock acquisition goes through the backend. Choosing the right one is usually the first architectural decision you'll make when adding Traffik to a project, and for Redis and Memcached specifically, choosing which *client* backs it is the second.

---

## Choosing a backend family

| Feature               | In-Memory                   | MultiProcess (experimental)         | Redis                                     | Memcached                                                         |
|-----------------------|------------------------------|--------------------------------------|--------------------------------------------|--------------------------------------------------------------------|
| Best for              | Dev, tests, single process   | Multiple workers, one machine        | Production, distributed                   | Existing Memcached stacks                                         |
| Persistence           | No                            | No                                    | Optional (`persistent=True`)              | Optional (`persistent=True` & `track_keys=True`(enables resets))  |
| Distributed           | No                            | Same machine only                    | Yes                                       | Yes                                                                |
| Lock type             | asyncio RLock                 | `multiprocessing.Semaphore`          | Redis Lua / Redlock / client-native lock  | Memcached `add` CAS                                               |
| Overhead              | Lowest                        | Low (shared memory, no network)      | Low (Lua scripts, pipelining)             | Low                                                                |
| Requires extra dep    | No                             | No (stdlib `multiprocessing`)         | See [Redis client backends](#redis-client-backends) | See [Memcached client backends](#memcached-client-backends) |
| `reset()` / `clear()` | Full                           | Full                                  | Full (Lua / native SCAN)                  | Only when `track_keys=True`                                       |

!!! warning "Lock TTL defaults to no expiry - set one"
    Every backend's `lock_ttl` defaults to `None` unless you configure `traffik.config.set_lock_ttl(...)` globally or pass `ttl=` explicitly to `backend.lock(...)`. For Memcached specifically, `None` means the lock key is stored with `exptime=0`, which is Memcached's protocol for *never expires* - not "expires immediately". If a process dies between acquiring a lock and releasing it (a crash, `kill -9`, OOM), an un-expiring lock stays held forever, and everything waiting on it deadlocks. Set a real TTL, sized comfortably longer than your slowest realistic critical section - see [Configuration](../configuration.md) for the global default, and pair it with `enforce_ttl_locally=True` so a critical section that runs *longer* than the TTL gets cancelled instead of silently losing exclusivity.

---

## In-Memory Backend

The `InMemoryBackend` stores everything in process memory using sharded `OrderedDict`
stores. It is not suitable for multi-process or distributed deployments, but it is perfect for development and testing.

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

!!! tip "Always use In-Memory in tests"
    Swap out your production backend for `InMemoryBackend` in your test suite. It
    requires no external services, resets itself cleanly between test runs, and adds
    no I/O latency. Your tests will be fast enough to make you smile.

---

## Multi-Process In-Memory Backend

!!! warning "Experimental"
    Works, and is tested, but the setup is unforgiving if you skip the constraints below. Read this whole section before using it in production.

Runs multiple worker processes on one machine, sharing rate-limit state without standing up Redis. Data lives in a `multiprocessing.shared_memory` segment; locking uses `multiprocessing.Semaphore`.

That second detail is what drives every constraint here: shared memory can be *attached to by name* from any process, but semaphores can't. They only work when created once and inherited through `fork()`. Hence, there is exactly one correct way to use this backend:

```python
# main.py - imported once by your process manager's parent, before it forks
from fastapi import FastAPI
from traffik.backends.multiprocess import MultiProcessInMemoryBackend

backend = MultiProcessInMemoryBackend(namespace="myapp")
backend.start()  # synchronous, no event loop needed

app = FastAPI(lifespan=backend.lifespan)
```

Run it with gunicorn, `preload_app=True`, and a fork-based worker class:

```python
# gunicorn_config.py
preload_app = True
workers = 4
worker_class = "uvicorn_worker.UvicornWorker"   # Needs `uvicorn-worker` installed
```

```bash
gunicorn -c gunicorn_config.py main:app
```

Every forked worker inherits a fully-working copy automatically - shared memory, semaphores, all of it. Workers don't need to call anything to "join" it; `lifespan=backend.lifespan` running in each worker's own event loop is enough.

!!! danger "Don't use `uvicorn --workers N` with this backend"
    Uvicorn's native multi-worker mode spawns workers with Python's `spawn` start method, not `fork`. Each worker re-imports your app fresh, with no shared memory inheritance at all. You'd get one independent, uncoordinated instance per worker, silently. Use gunicorn (or another genuinely fork-based process manager) instead.

### Why `start()` *and* `initialize()`

`start()` does the actual setup (shared memory, semaphores, lock pools) and is fully synchronous, so it's safe to call from gunicorn's `preload_app` phase, which doesn't have an event loop running. `initialize()` calls `start()` (a no-op if the parent already did it) and additionally makes sure *this process's* background cleanup task is running in *this process's* event loop, which is something `start()` genuinely can't do without a loop, and something each worker needs done independently regardless of what the parent already did. `lifespan=backend.lifespan` calls `initialize()` for you on ASGI startup, so in the common case you never call it directly.

### What you get for free

- **Fork-safety for threads.** The backend registers an `os.register_at_fork` hook that rebuilds its internal thread pool in every forked child - POSIX `fork()` only duplicates the calling thread, so the parent's worker threads simply don't exist post-fork, and this handles that transparently.
- **Safe shutdown.** Only the process that actually called `start()` will ever unlink the shared memory segment on `close()`. An ordinary worker recycling (e.g. gunicorn's `max_requests`) never tears down state the other workers still need.
- **Self-healing on restart.** If a segment with the target name already exists when `start()` runs, it's treated as a stale leftover from a previous run (never an actively-used peer - nothing can safely attach to one anyway) and recreated.

---

## Redis client backends

Traffik ships **two** independent Redis backend implementations, one per client
library. Both are named `RedisBackend`, live under `traffik.backends.redis`, and
implement the identical `ThrottleBackend` interface - pick one and the rest of your
code (throttles, quotas, middleware) doesn't change. You do not need both installed.

```python
# redis-py (the "aioredis" module) - default, most common
from traffik.backends.redis.aioredis import RedisBackend

# coredis - native cluster & sentinel support
from traffik.backends.redis.coredis import RedisBackend
```

For backwards compatibility, `from traffik.backends.redis import RedisBackend`
still works and resolves to the `aioredis`-based backend if it's installed.

| Feature                | `redis.aioredis` (redis-py)        | `redis.coredis`                                  |
|-------------------------|-------------------------------------|---------------------------------------------------|
| Underlying client       | `redis.asyncio` (`redis` package)  | `coredis`                                          |
| Redis Cluster           | No (single node / `redis`-side proxy only) | Yes, native (`coredis.RedisCluster`)       |
| Redis Sentinel          | No                                   | Yes, native (`coredis.Sentinel`)                   |
| Distributed lock types  | `"redis"` (SET NX EX + Lua) and `"redlock"` (via `pottery`) | Single client-native lock (`coredis.patterns.lock.Lock`) |
| Extra dependencies      | `redis>=5.0.0`, `pottery>=3.0.1`   | `coredis>=6.0`                                     |
| Python support          | Same as Traffik                     | Python **3.10+** only                              |
| Install extra           | `traffik[aioredis]` (alias: `traffik[redis]`) | `traffik[coredis]`                    |

**When to pick which:**

- Default to **`aioredis`** for a single Redis instance or a standard Redis
  deployment behind a proxy. It's the more battle-tested client, and `pottery`'s
  Redlock support covers the multi-instance-without-native-clustering case.
- Reach for **`coredis`** if you're running an actual Redis Cluster or Sentinel
  topology and want the backend talking to it natively, instead of routing
  through a single endpoint. It requires Python 3.10+.

### `redis.aioredis`

```python
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    persistent=False,
    on_error="throttle",
    lock_type="redis",      # "redis" (default) or "redlock"
    lock_blocking=True,
    lock_ttl=10.0, # Never set to "0" to avoid deadlocks
    lock_blocking_timeout=None,
)
```

When you need full control over the connection (connection pools, TLS, password, etc.),
pass an async factory instead of a URL:

```python
import redis.asyncio as aioredis
from traffik.backends.redis.aioredis import RedisBackend

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

**Lock types:**

| `lock_type` | Algorithm                           | Best for                              |
|-------------|--------------------------------------|----------------------------------------|
| `"redis"`   | SET NX EX + Lua fencing              | Single Redis instance, lowest latency |
| `"redlock"` | Redlock (via `pottery.AIORedlock`)   | Redis clusters, multiple instances    |

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

### `redis.coredis`

Supports three connection topologies via the same `connection` argument.

**Single node:**

```python
from traffik.backends.redis.coredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
```

**Redis Cluster** - pass a pre-built client, or a list of startup nodes and let the
backend build the cluster client for you:

```python
from coredis import RedisCluster
from coredis.connection import TCPLocation
from traffik.backends.redis.coredis import RedisBackend

client = RedisCluster(
    startup_nodes=[TCPLocation("127.0.0.1", 7000)],
    decode_responses=True,
)
backend = RedisBackend(client, namespace="myapp")

# or, equivalently, hand the backend the node list directly:
backend = RedisBackend(
    [TCPLocation("127.0.0.1", 7000), TCPLocation("127.0.0.1", 7001)],
    namespace="myapp",
)
```

**Sentinel:**

```python
from coredis import Sentinel
from traffik.backends.redis.coredis import RedisBackend

sentinel = Sentinel([("sentinel-host", 26379)], stream_timeout=0.1)
backend = RedisBackend(
    sentinel,
    sentinel_service_name="myredis",
    namespace="myapp",
)
```

Or pass an async factory `() -> Union[Redis, RedisCluster]` for full control over
client construction, the same way `aioredis` supports it.

**Characteristics:**

- Any pre-built `coredis.Redis`/`coredis.RedisCluster` instance handed in must be
  constructed with `decode_responses=True`, or initialization raises.
- No `lock_type` choice - locking always goes through `coredis.patterns.lock.Lock`,
  which is fencing-token protected but not Redlock-style multi-instance quorum locking.
- `lock_sleep` controls the poll interval between blocking-lock acquisition
  attempts (default `0.0025`s), independent of `lock_blocking_timeout`.
- `lock_contention_threshold` gates high-contention keys through a process-local
  `asyncio.Lock` before hitting Redis, reducing thundering-herd load on the
  server when many local tasks are waiting on the same key.
- A pre-built client or Sentinel-provided primary is never closed by `close()` -
  only clients the backend constructed itself are torn down, so ownership stays
  with whoever passed the client in.

---

## Memcached client backends

Same story as Redis: two backend implementations, one per client library, both
named `MemcachedBackend`, both under the same `ThrottleBackend` interface.

```python
# aiomcache - default, single node only
from traffik.backends.memcached.aiomcache import MemcachedBackend

# emcache - multi-node, higher throughput, no Windows support
from traffik.backends.memcached.emcache import MemcachedBackend
```

`from traffik.backends.memcached import MemcachedBackend` still works for backwards
compatibility and resolves to the `aiomcache`-based backend if installed.

| Feature                | `memcached.aiomcache`              | `memcached.emcache`                                |
|-------------------------|--------------------------------------|-----------------------------------------------------|
| Underlying client       | `aiomcache`                          | `emcache`                                            |
| Multi-node support       | No, single node only                | Yes, native (Rendezvous hashing across nodes)        |
| TLS / auth               | No                                    | Yes (`ssl`, `ssl_verify`, `ssl_extra_ca`, `username`, `password`) |
| Autobatching             | No                                    | Yes, optional (`autobatching=True`) - concurrent `get`s transparently batched into `get_many` |
| Platform support         | All (Windows included)              | Linux and macOS only (no Windows wheels)             |
| Extra dependency         | `aiomcache>=0.8.2`                   | `emcache>=1.3.3`                                     |
| Install extra            | `traffik[aiomcache]` (alias: `traffik[memcached]`) | `traffik[emcache]`                     |

**When to pick which:**

- Default to **`aiomcache`** for a single Memcached instance, or if you need
  Windows support.
- Reach for **`emcache`** if you're spreading load across multiple Memcached
  nodes, need TLS/auth, or want autobatching for read-heavy workloads - and can
  run on Linux/macOS.

### `memcached.aiomcache`

```python
from traffik.backends.memcached.aiomcache import MemcachedBackend

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

- Does not support multi-node setups - `host`/`port`/`url` always point at a
  single Memcached server.
- Locks are implemented using Memcached's atomic `add` operation (add only succeeds
  if the key does not exist), with a fencing token for ownership verification.
- Locks are instance-bound, not task-reentrant the way Redis locks are.
- Memcached keys are limited to 250 bytes, keep your `namespace` short.

### `memcached.emcache`

```python
from traffik.backends.memcached.emcache import MemcachedBackend

# Single node
backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace=":memcached:",
)

# Multiple nodes - traffic distributed via Rendezvous hashing
backend = MemcachedBackend(
    nodes=[("memcached-1", 11211), ("memcached-2", 11211), "memcached-3:11211"],
    max_connections=4,
    min_connections=1,
    namespace=":memcached:",
)

# TLS + auth
backend = MemcachedBackend(
    host="memcached.internal",
    port=11211,
    ssl=True,
    ssl_verify=True,
    username="myapp",
    password="secret",
    namespace=":memcached:",
)
```

**Characteristics:**

- `nodes` takes precedence over `url`/`host`/`port` when provided; each element
  may be a `(host, port)` tuple, a `"memcached://host:port"` URL, or a
  `"host:port"` string.
- `max_connections`/`min_connections` and `purge_unused_connections_after`
  configure emcache's adaptive per-node connection pool; `connection_timeout`
  bounds how long opening a new connection may take.
- `autobatching=True` transparently coalesces concurrent `get` calls into a
  single `get_many` round-trip - useful under high read concurrency, off by default.
- Only supported on Linux and macOS (`emcache` ships no Windows wheels); use
  `aiomcache` if you need Windows support.

### The `track_keys` limitation

Memcached has no equivalent of Redis `SCAN`, so Traffik cannot natively list all keys in a namespace. The `clear()` method, called during non-persistent context teardown, is therefore a **no-op by default** on both Memcached backends.

Enable `track_keys=True` to have Traffik maintain a side-car key(s) that records every key it sets:

```python
backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace=":memcached:",
    track_keys=True,  # enables `clear()` at the cost of extra writes
    number_of_tracking_shards=16,  # spread tracking writes to reduce contention
)
```

!!! warning "`track_keys=True` adds a little overhead, per write, not per namespace"
    Enabling this adds one atomic `append` (or, on the very first write to a shard, an `add`) alongside every real write - no read-before-write involved, so it doesn't race under concurrent writes the way a naive get-modify-set tracking scheme would. Keys are spread across multiple tracking shards (`number_of_tracking_shards`, default 16) rather than one, so a busy namespace doesn't concentrate contention on a single tracking key either. Still not free, so only enable it if you genuinely need `clear()` to work on Memcached. An alternative if the Memcached instance is dedicated to Traffik, is to override `clear()` in a subclass to call `flush_all()` on the Memcached client instead.

---

## Installing client dependencies

Redis and Memcached support are optional - install only the client(s) you need:

| Extra                  | Installs                    | Backend                              |
|--------------------------|------------------------------|----------------------------------------|
| `traffik[redis]`         | `redis`, `pottery`           | `traffik.backends.redis.aioredis`     |
| `traffik[aioredis]`      | `redis`, `pottery`           | `traffik.backends.redis.aioredis`     |
| `traffik[coredis]`       | `coredis` (Python 3.10+)     | `traffik.backends.redis.coredis`      |
| `traffik[memcached]`     | `aiomcache`                  | `traffik.backends.memcached.aiomcache`|
| `traffik[aiomcache]`     | `aiomcache`                  | `traffik.backends.memcached.aiomcache`|
| `traffik[emcache]`       | `emcache` (Linux/macOS only) | `traffik.backends.memcached.emcache`  |
| `traffik[all]`           | All of the above             | All backends                          |

```bash
pip install "traffik[coredis]"
# or, for everything:
pip install "traffik[all]"
```

`traffik[redis]` and `traffik[memcached]` are convenience aliases for the default
client of each family (`aioredis` and `aiomcache` respectively) - use them if you
don't have a specific reason to prefer the alternative client.

---

## Backend lifecycle

Every backend needs to be started before use and closed when you are done. Traffik provides three patterns; pick the one that fits your framework.

=== "Lifespan context manager (recommended)"

    ```python
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from traffik.backends.redis.aioredis import RedisBackend

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

By default, `persistent=False`, Traffik calls `reset()` on the backend when the context exits. This wipes all throttle counters, which can be what you usually want between test runs and application restarts.

Set `persistent=True` to keep counter state alive across restarts:

```python
backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    persistent=True,  # counters survive restarts
)
```

### The `on_error` parameter

Every backend shares the same error-handling knob:

| Value        | Behaviour on backend error                                    |
|--------------|-----------------------------------------------------------------|
| `"throttle"` | Treat the request as if it exceeded the limit (safe default)   |
| `"allow"`    | Let the request through (optimistic)                            |
| `"raise"`    | Propagate the exception to your exception handler               |
| `callable`   | Call your function `(connection, exc_info) -> wait_ms`          |
