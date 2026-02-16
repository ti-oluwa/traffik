# Performance Tips

Traffik is designed to be performant out of the box. But like all systems, there are knobs to turn when you need to squeeze out more. This page covers the most impactful optimizations, roughly ordered by impact.

---

## Quick Wins Checklist

Before diving into specifics, here are the changes that have the most impact for the least effort:

- [ ] Use >= 1 second rate windows (eliminates lock overhead for FixedWindow/SlidingWindowCounter)
- [ ] Enable InMemory shard striping: `InMemoryBackend(number_of_shards=32)`
- [ ] Use connection pooling: `MemcachedBackend(pool_size=20)`
- [ ] Don't call DB/external APIs inside identifier functions
- [ ] Exempt trusted internal clients with `EXEMPTED` (zero backend overhead)
- [ ] Keep `cache_ids=True` (default) — especially for WebSockets

---

## 1. Use the Right Backend

The backend choice has a larger impact on performance than any other single factor.

| Deployment | Backend | Notes |
|---|---|---|
| Single-process (dev, small apps) | `InMemoryBackend` | Zero network overhead, fastest possible |
| Multi-process, single machine | `InMemoryBackend` + replication | State doesn't share between processes |
| Distributed (multiple nodes) | `RedisBackend` | Network round-trip, but accurate distributed counting |
| Already have Memcached | `MemcachedBackend` | Comparable to Redis, no scripting support |
| Redis + memory efficiency | `RedisBackend` | Scripts cached on server, minimal overhead |

Don't use `RedisBackend` for a single-process application just because "Redis is production-grade." The network round-trip will cost you 1-5ms per request unnecessarily. `InMemoryBackend` is genuinely the right tool for single-process deployments.

```python
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend

# Development / single-process
backend = InMemoryBackend(namespace="myapp")

# Production / distributed
backend = RedisBackend("redis://localhost:6379", namespace="myapp")
```

---

## 2. Choose Your Strategy Wisely

Strategies differ significantly in performance. Here's the order from fastest to slowest:

| Strategy | Overhead | Accuracy | Best For |
|---|---|---|---|
| `FixedWindow` (>= 1s window) | Minimal — single atomic op, no lock | Good | General APIs, high throughput |
| `SlidingWindowCounter` | Low — two atomic ops | Better | When burst boundaries matter |
| `TokenBucket` | Medium — lock + read/write | Excellent (burst-aware) | APIs with natural burst patterns |
| `LeakyBucket` | Medium — lock + read/write | Excellent (smooth) | Enforcing constant rate |
| `SlidingWindowLog` | High — serialization + cleanup | Perfect | Strict correctness, lower traffic |

The default strategy is `FixedWindow`. For most applications, it's the right choice — it's accurate enough, and the performance is hard to beat.

```python
from traffik import HTTPThrottle
from traffik.strategies import FixedWindow, TokenBucket

# Fastest: FixedWindow with a >= 1s window
throttle = HTTPThrottle("api", rate="1000/min", strategy=FixedWindow())

# More nuanced: TokenBucket for burst-friendly limiting
throttle = HTTPThrottle("api", rate="100/sec", strategy=TokenBucket())
```

---

## 3. Configure Lock Striping (InMemory)

`InMemoryBackend` uses internal locks to prevent race conditions. By default it uses a small number of shards. Increasing the shard count distributes lock contention across more buckets, dramatically improving throughput under high concurrency:

```python
from traffik.backends.inmemory import InMemoryBackend

# Default: small number of shards (low concurrency overhead)
backend = InMemoryBackend(namespace="myapp")

# High concurrency: 32 shards distribute lock contention
backend = InMemoryBackend(namespace="myapp", number_of_shards=32)
```

A good rule of thumb: set `number_of_shards` to roughly your expected peak concurrent requests / 4. For most applications, 16-64 shards is a good range.

---

## 4. Use Connection Pooling (Memcached)

Every throttle check requires a backend operation. Without connection pooling, each operation opens and closes a network connection — this is expensive. Use pooling:

```python
from traffik.backends.memcached import MemcachedBackend

# Default: small pool
backend = MemcachedBackend(host="localhost", port=11211, namespace="myapp")

# Production: larger pool to handle concurrent requests
backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace="myapp",
    pool_size=20,   # Tune based on expected concurrency
)
```

For Redis, `aioredis` (used by `RedisBackend`) handles connection pooling internally. You can tune it via the connection URL or by passing a pre-configured client.

---

## 5. Keep Identifier Functions Cheap

The identifier function runs on **every request** to determine who is being throttled. A slow identifier function adds latency proportional to your traffic.

```python
# Fast: read from request headers (in-memory, no I/O)
async def fast_identifier(request: Request):
    return request.headers.get("X-API-Key") or request.client.host

# Slow: database lookup on every request (don't do this!)
async def slow_identifier(request: Request):
    api_key = request.headers.get("X-API-Key")
    user = await db.fetch("SELECT id FROM users WHERE api_key = $1", api_key)
    return user["id"]  # Every request = one DB query
```

If you need to look up user information, cache it in the request state after the first lookup, or do the lookup once in authentication middleware and store the result in `request.state.user_id`.

---

## 6. Avoid Logging in Backend Operations

This sounds minor but it isn't. Logging is synchronous I/O and can block the async event loop under load.

```python
# This is inside the hot path — every request goes through here
async def my_backend_increment(self, key: str, amount: int = 1) -> int:
    # BAD: logging on every request
    logger.debug(f"Incrementing {key} by {amount}")
    result = await self._client.incr(key, amount)
    logger.debug(f"New value: {result}")
    return result

# Better: no logging in the hot path
async def my_backend_increment(self, key: str, amount: int = 1) -> int:
    return await self._client.incr(key, amount)
```

According to our benchmarks, adding `logger.debug()` calls inside backend operations produces a **~10x slowdown** on high-concurrency workloads. Log in error handlers (which only run on failures), not in the normal execution path.

---

## 7. Exempt Trusted Clients with EXEMPTED

For trusted internal services, health check endpoints, or admin users, returning `EXEMPTED` from the identifier completely bypasses throttling — no counter read, no counter write, no lock. Zero overhead:

```python
from traffik import EXEMPTED

INTERNAL_TOKEN = "my-internal-service-token"

async def smart_identifier(request: Request):
    # Internal services: completely bypassed
    if request.headers.get("X-Internal-Token") == INTERNAL_TOKEN:
        return EXEMPTED

    # Everyone else: throttled by API key
    return request.headers.get("X-API-Key") or request.client.host


throttle = HTTPThrottle("api", rate="1000/min", identifier=smart_identifier)
```

This is especially useful for health check endpoints that get hammered by load balancers. There's no point counting those requests — exempt them and save the backend round-trip.

---

## 8. Enable Identifier Caching (It's Default)

`cache_ids=True` is the default and caches the computed identifier on the connection's `state` object. For WebSocket connections especially, this prevents recomputing the identifier on every message:

```python
# cache_ids=True (default) — computes identifier once, caches on connection.state
throttle = HTTPThrottle("api", rate="100/min", cache_ids=True)

# cache_ids=False — recomputes identifier on every hit
throttle = HTTPThrottle("api", rate="100/min", cache_ids=False)
```

Only disable `cache_ids` if your identifier might change during a connection's lifetime (rare). For everything else, leave it enabled.

---

## 9. Sort Middleware Throttles Cheap First

When using multiple throttles in middleware, put the cheapest (lowest limit, most likely to fire) first. A throttle that rejects a request early prevents the more expensive throttles from running at all:

```python
from traffik.middleware import ThrottleMiddleware

# Burst throttle (cheap, low limit) runs first
# Sustained throttle (more expensive, higher limit) only runs if burst passes
app.add_middleware(
    ThrottleMiddleware,
    throttles=[burst_throttle, sustained_throttle],
    # cheap_first=True is the default — throttles are automatically sorted
)
```

Traffik's middleware already applies `cheap_first` ordering by default, so this is mostly about being deliberate when configuring multiple throttles.

---

## 10. Avoid Sub-Second Rate Windows in Production

Sub-second windows (e.g., `"100/500ms"`, `"10/100ms"`) trigger locking in `FixedWindow` and `SlidingWindowCounter` — even when they would otherwise be lock-free. This is necessary for accuracy at millisecond precision, but it adds distributed lock overhead on every request.

```python
# Lock-free: uses atomic increment_with_ttl only
throttle = HTTPThrottle("api", rate="6000/min")    # 60s window >= 1s

# Requires locking: sub-second window
throttle = HTTPThrottle("api", rate="100/500ms")   # 500ms < 1s
```

In most cases, you can express the same intent with a >= 1s window and get better performance:

```python
# These are semantically equivalent for burst protection:
throttle = HTTPThrottle("api", rate="20/sec")       # 1s window, no lock overhead
# vs.
throttle = HTTPThrottle("api", rate="10/500ms")     # 500ms window, adds lock overhead
```

Use sub-second windows when you genuinely need millisecond-level burst control. Otherwise, stick to seconds.

---

## Putting It All Together

Here's a production-optimized configuration for a high-traffic API:

```python
from traffik import HTTPThrottle, EXEMPTED
from traffik.backends.redis import RedisBackend
from traffik.strategies import FixedWindow

# Redis backend (distributed, production-grade)
backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    on_error="allow",  # Fail open on backend errors (tune per your risk tolerance)
)

# Efficient identifier: fast header read, exempt internal services
async def api_identifier(request):
    if request.headers.get("X-Internal") == "true":
        return EXEMPTED
    return request.headers.get("X-API-Key") or request.client.host

# Fast strategy: FixedWindow with >= 1s window = lock-free path
throttle = HTTPThrottle(
    "api:v1",
    rate="1000/min",          # 60s window, no lock overhead
    strategy=FixedWindow(),
    identifier=api_identifier,
    cache_ids=True,            # Cache identifier per connection
    backend=backend,
)
```
