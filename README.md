# Traffik

Distributed rate limiting for FastAPI and Starlette. Handles everything from a single dev server to multi-process workers sharing state over Redis, Memcached, or POSIX shared memory whil still using the same throttle code throughout.

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Installation

```bash
# In-memory only (no extra dependencies)
pip install traffik

# Redis via redis.asyncio (the old `redis` extra still works for backwards compatibility)
pip install "traffik[aioredis]"
# or
pip install "traffik[redis]"  # alias that installs aioredis

# Redis via coredis (supports clusters and Sentinel)
pip install "traffik[coredis]"

# Memcached via aiomcache (the old `memcached` extra still works)
pip install "traffik[aiomcache]"
# or
pip install "traffik[memcached]" # alias that installs aiomcache

# Memcached via emcache - Linux and macOS only (high-performance, multi-node)
pip install "traffik[emcache]"

# Everything at once
pip install "traffik[all]"
```

> **Note on the compatibility aliases:** `traffik[redis]` and `traffik[memcached]` were the original extras from 1.1.x. They still work and resolve to `aioredis` and `aiomcache` respectively, so existing setups don't break. The new extras (`aioredis`, `coredis`, `aiomcache`, `emcache`) are explicit about which client library they pull in.

---

## Quickstart

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:v1", rate="100/min")

@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": []}
```

Swap the backend for Redis or Memcached in production. The throttle code stays exactly the same.

---

## Backends

### In-memory (development / single machine and worker)

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)
```

Fast, zero dependencies. State lives in one process, so it won't work correctly across multiple workers. Fine for development and single-worker deployments.

### Multi-process shared memory (multiple workers, same machine)

When you're running gunicorn or uvicorn with multiple workers (on a single machine) and you don't want to stand up Redis just to get accurate counters:

```python
from traffik.backends.multiprocess import MultiProcessInMemoryBackend

# Create before forking in your app factory or gunicorn config
backend = MultiProcessInMemoryBackend.create(
    namespace="myapp",
    max_keys=65536,
    number_of_shards=64,       # rule of thumb: 2 × worker count
    cleanup_frequency=30.0,    # reclaim expired slots every 30s (might add some overhead)
)
app = FastAPI(lifespan=backend.lifespan)
```

Workers connect to the same POSIX shared memory segment after fork. Counters are accurate across all of them with no network round-trips. Requires Linux or macOS with the `fork` start method (the default on Linux; use `multiprocessing.set_start_method("fork")` on macOS).

In a worker that needs to attach to the existing backend/segment:

```python
backend = MultiProcessInMemoryBackend.attach(namespace="myapp")
```

For strategies that store variable-length blobs (sliding window log, token bucket with history), size `max_value_size` accordingly. The default 512 bytes handles up to ~17 requests per window for sliding window log.

### Redis via `redis.asyncio`

```python
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)
```

### Redis via `coredis` (clusters, Sentinel)

```python
from traffik.backends.redis.coredis import RedisBackend

# Single node
backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")

# Cluster - pass startup nodes
from coredis.connection import TCPLocation
backend = RedisBackend(
    [TCPLocation("127.0.0.1", 7000), TCPLocation("127.0.0.1", 7001)],
    namespace="myapp",
)

# Sentinel
from coredis import Sentinel
sentinel = Sentinel([("sentinel-host", 26379)])
backend = RedisBackend(sentinel, sentinel_service_name="myredis", namespace="myapp")
```

### Memcached via `aiomcache`

```python
from traffik.backends.memcached.aiomcache import MemcachedBackend

backend = MemcachedBackend(host="localhost", port=11211, namespace="myapp")
```

### Memcached via `emcache` (Linux / macOS, multi-node)

```python
from traffik.backends.memcached.emcache import MemcachedBackend

backend = MemcachedBackend(
    nodes=["memcached://node1:11211", "memcached://node2:11211"],
    namespace="myapp",
)
```

`emcache` uses Rendezvous hashing for node distribution and an adaptive connection pool. Noticeably faster than aiomcache at high throughput.

---

## Strategies

All strategies work with all backends. The same `FixedWindow()` on an in-memory backend during dev runs identically on Redis in production.

```python
from traffik.strategies import (
    FixedWindow,           # simple, memory-efficient, slight boundary burst
    SlidingWindowCounter,  # two-counter weighted approximation, good balance
    SlidingWindowLog,      # exact timestamps, higher memory
    TokenBucket,           # allows controlled bursts
    TokenBucketWithDebt,   # token bucket with configurable overdraft
    LeakyBucket,           # smooth output, no bursts allowed
    LeakyBucketWithQueue,  # strict FIFO ordering
    GCRA,                  # Generic Cell Rate Algorithm, perfectly smooth
)

throttle = HTTPThrottle("api", rate="100/min", strategy=SlidingWindowLog())
```

There are also advanced strategies in `traffik.strategies.custom`: `TieredRateStrategy`, `AdaptiveThrottleStrategy`, `GCRAStrategy`, `DistributedFairnessStrategy`, `GeographicDistributionStrategy`, and more.

---

## Rate formats

```python
"100/min"           # 100 per minute
"5/s"               # 5 per second
"10/30s"            # 10 per 30 seconds
"1000/hour"
"500/day"
"200/500ms"         # sub-second windows
Rate(limit=50, minutes=1)   # explicit Rate object
```

---

## Integration patterns

### FastAPI dependency

```python
@app.get("/search", dependencies=[Depends(throttle)])
async def search():
    ...
```

### FastAPI decorator (keeps OpenAPI schema clean)

```python
from traffik.decorators import throttled

burst = HTTPThrottle("api:burst", rate="20/s")
sustained = HTTPThrottle("api:sustained", rate="500/min")

@app.post("/upload")
@throttled(burst, sustained)
async def upload(request: Request):
    ...
```

### Middleware (blanket rules across routes)

```python
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(
            HTTPThrottle("api:global", rate="1000/min"),
            path="/api/",
        ),
        MiddlewareThrottle(
            HTTPThrottle("api:writes", rate="100/min"),
            path="/api/",
            methods={"POST", "PUT", "PATCH", "DELETE"},
        ),
    ],
    backend=backend,
)
```

### WebSockets

```python
from traffik.throttles import WebSocketThrottle, is_throttled

ws_throttle = WebSocketThrottle("ws:messages", rate="30/min")

@app.websocket("/ws", dependencies=[Depends(ws_throttle)]) # Gate connection itself
async def ws_endpoint(websocket: WebSocket):
    async with websocket_backend(websocket.app):
        await websocket.accept()
        while True:
            data = await websocket.receive_text()
            await ws_throttle.hit(websocket, context={"scope": "message"})
            if is_throttled(websocket):
                await websocket.send_text("Throttled! Please wait before sending more messages.")
                continue
            await websocket.send_text(process(data))
```

---

## Custom identifiers

By default, traffik uses the client IP. Key on anything you want:

```python
async def api_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or request.client.host

throttle = HTTPThrottle("api", rate="100/min", identifier=api_key)
```

Return the `EXEMPTED` sentinel to let specific connections through unconditionally:

```python
from traffik.types import EXEMPTED

async def identifier(request: Request):
    if request.headers.get("X-Internal-Token") == SECRET:
        return EXEMPTED
    return request.client.host
```

---

## Cost-based throttling

Not all requests are equal. A bulk export should count more than a simple read:

```python
async def request_cost(request: Request, context=None) -> int:
    if "/export" in request.url.path:
        return 10
    return 1

throttle = HTTPThrottle("api", rate="100/min", cost=request_cost)

# Or override at call time
await throttle.hit(request, cost=5)
```

---

## Response headers

```python
from traffik.headers import Headers, Header

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    headers=Headers({
        "X-RateLimit-Limit":     Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "Retry-After":           Header.RESET_SECONDS(when="throttled"),
    }),
)
```

You can also resolve headers manually in a custom throttled handler:

```python
from traffik.exceptions import ConnectionThrottled

async def my_handler(request, wait_ms, throttle, context):
    headers = await throttle.get_headers(request, context=context)
    raise ConnectionThrottled(wait_period=int(wait_ms / 1000), headers=headers)
```

---

## Rules and bypass

Apply a throttle only when certain conditions are met, or skip it entirely for others:

```python
from traffik.registry import ThrottleRule, BypassThrottleRule

# Only throttle write methods on the global limit
write_throttle.add_rules(
    "api:global",
    ThrottleRule(methods={"POST", "PUT", "PATCH", "DELETE"}),
)

# Skip throttle for internal traffic
async def is_internal(request: Request) -> bool:
    return request.headers.get("X-Internal") == "1"

throttle = HTTPThrottle(
    "api:global",
    rate="100/min",
    rules=[BypassThrottleRule(predicate=is_internal)],
)
```

---

## Deferred quota (QuotaContext)

Sometimes you only want to consume quota if the operation actually succeeds, or you want to consume several throttles (sort-of)atomically:

```python
async def create_order(request: Request):
    # Lock acquired on entry, quota consumed on clean exit only
    async with order_throttle.quota(request, lock=True) as quota:
        quota(cost=1)                        # uses owner throttle
        quota(heavy_ops_throttle, cost=5)    # different throttle

        result = await do_expensive_work()
    return result
```

Consecutive calls to the same throttle with the same config are automatically aggregated into a single backend operation, so `quota(cost=2); quota(cost=3)` is one `increment(key, 5)`, not two separate calls.

You can also check available quota without consuming it first:

```python
async with throttle.quota(request) as quota:
    if not await quota.check(cost=10):
        raise HTTPException(429, "Insufficient quota")
    quota(cost=10)
    result = await heavy_work()
```

---

## Error handling and resilience

Three built-in strategies for what to do when the backend throws:

```python
# Allow all requests through on backend failure
throttle = HTTPThrottle("api", rate="100/min", on_error="allow")

# Throttle all requests on backend failure (safe default)
throttle = HTTPThrottle("api", rate="100/min", on_error="throttle")

# Raise the exception - handle it yourself
throttle = HTTPThrottle("api", rate="100/min", on_error="raise")
```

Or use the built-in failover handler to fall back to a secondary backend with an automatic circuit breaker:

```python
from traffik.error_handlers import failover, CircuitBreaker
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis.aioredis import RedisBackend

primary = RedisBackend("redis://primary:6379", namespace="myapp")
fallback = InMemoryBackend(namespace="fallback")

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    backend=primary,
    on_error=failover(
        backend=fallback,
        breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30.0),
    ),
)
```

After 5 consecutive Redis failures the circuit opens. New requests immediately fall back to the in-memory backend without waiting for Redis. The circuit transitions to half-open after 30 seconds, lets a probe through, and closes again on success.

There's also a `retry` handler for transient errors:

```python
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    on_error=retry(
        max_retries=3, 
        retry_delay=0.05, 
        retry_on=(TimeoutError,)
    ),
)
```

---

## Dynamic backends (multi-tenant)

Route different tenants to different backends entirely at runtime without creating a new throttle per tenant:

```python
throttle = HTTPThrottle("api", rate="100/min", dynamic_backend=True)

@app.get("/data")
async def data(request: Request):
    tenant_backend = get_backend_for_tenant(request)
    async with tenant_backend(request.app):
        return await throttle.hit(request)
```

---

## Runtime updates

Everything can be updated without recreating the throttle:

```python
await throttle.update_rate("200/min")
await throttle.update_strategy(TokenBucket())
await throttle.update_cost(5)
await throttle.disable()    # let all requests through temporarily
await throttle.enable()
```

The registry gives you global control:

```python
from traffik.registry import GLOBAL_REGISTRY

await GLOBAL_REGISTRY.disable_all()   # maintenance mode
await GLOBAL_REGISTRY.enable_all()
```

---

## Strategy statistics

Check the current state without consuming quota. This is useful for pre-checks and monitoring:

```python
stat = await throttle.stat(request)
if stat:
    print(f"{stat.hits_remaining} hits left, {stat.wait_ms}ms until reset")
    print(stat.metadata)  # strategy-specific details
```

Or a simple boolean check:

```python
if not await throttle.check(request, cost=5):
    raise HTTPException(429, "Not enough quota for this operation")
```

---

## Full documentation

[https://ti-oluwa.github.io/traffik/](https://ti-oluwa.github.io/traffik/)

Covers advanced strategies (tiered rates, adaptive throttling, GCRA, distributed fairness, geographic distribution), WebSocket per-message throttling, testing patterns, performance benchmarks, and the full API reference.

## License

MIT
