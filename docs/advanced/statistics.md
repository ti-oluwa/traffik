# Strategy Statistics

Sometimes you want to *look* at the rate limit counter without actually *touching* it. Maybe you want to add `X-RateLimit-Remaining` headers to every response. Maybe you want a `/usage` endpoint that tells clients where they stand. Maybe you want to feed data into Prometheus.

That's what `throttle.stat()` is for.

!!! tip "stat() is read-only"
    Calling `stat()` never consumes quota. It reads the current state from the backend and returns it. Your clients can call a stats endpoint as often as they like — it won't move the rate limit needle.

---

## Basic Usage

```python
stat = await throttle.stat(request, context={...})
```

`stat()` returns a `StrategyStat` object, or `None` if the strategy doesn't support stats (custom strategies) or if the client is exempt from throttling.

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:read", rate="100/min", backend=backend)

@app.get("/items")
async def get_items(request: Request = Depends(throttle)):
    stat = await throttle.stat(request)
    if stat:
        print(f"Hits remaining: {stat.hits_remaining}")
        print(f"Wait time: {stat.wait_ms}ms")
    return {"items": [...]}
```

---

## StrategyStat Fields

| Field | Type | Description |
|---|---|---|
| `key` | `Stringable` | The full namespaced throttle key for this client |
| `rate` | `Rate` | The rate limit definition (e.g., 100 requests/minute) |
| `hits_remaining` | `float` | Quota left in the current window. Can be `inf` for unlimited. |
| `wait_ms` | `float` | Milliseconds until the client can send again (0 if not throttled) |
| `metadata` | `TypedDict \| None` | Strategy-specific data (window timestamps, token counts, etc.) |

---

## Typed Metadata Per Strategy

Each built-in strategy exposes rich metadata you can use for observability.

=== "FixedWindow"

    ```python
    from traffik.strategies.fixed_window import FixedWindowStatMetadata

    stat = await throttle.stat(request)
    if stat and stat.metadata:
        meta: FixedWindowStatMetadata = stat.metadata
        # meta["strategy"]          -> "fixed_window"
        # meta["window_start_ms"]   -> window start (Unix ms)
        # meta["window_end_ms"]     -> window end (Unix ms)
        # meta["current_count"]     -> requests so far in this window
    ```

=== "SlidingWindowLog"

    ```python
    from traffik.strategies.sliding_window import SlidingWindowLogStatMetadata

    stat = await throttle.stat(request)
    if stat and stat.metadata:
        meta: SlidingWindowLogStatMetadata = stat.metadata
        # meta["strategy"]          -> "sliding_window_log"
        # meta["window_start_ms"]   -> rolling window start
        # meta["entry_count"]       -> number of log entries in window
        # meta["current_cost_sum"]  -> total cost in window
        # meta["oldest_entry_ms"]   -> timestamp of oldest entry
    ```

=== "SlidingWindowCounter"

    ```python
    from traffik.strategies.sliding_window import SlidingWindowCounterStatMetadata

    stat = await throttle.stat(request)
    if stat and stat.metadata:
        meta: SlidingWindowCounterStatMetadata = stat.metadata
        # meta["strategy"]          -> "sliding_window_counter"
        # meta["current_window_id"] -> current window identifier
    ```

=== "TokenBucket"

    ```python
    from traffik.strategies.token_bucket import TokenBucketStatMetadata

    stat = await throttle.stat(request)
    if stat and stat.metadata:
        meta: TokenBucketStatMetadata = stat.metadata
        # meta["strategy"]             -> "token_bucket"
        # meta["tokens"]               -> current token count
        # meta["capacity"]             -> bucket capacity (burst size)
        # meta["refill_rate_per_ms"]   -> tokens added per millisecond
    ```

=== "LeakyBucket"

    ```python
    from traffik.strategies.leaky_bucket import LeakyBucketStatMetadata

    stat = await throttle.stat(request)
    if stat and stat.metadata:
        meta: LeakyBucketStatMetadata = stat.metadata
        # meta["strategy"] -> "leaky_bucket"
    ```

---

## Adding Rate Limit Headers to Responses

The most common use of stats is adding standard `X-RateLimit-*` headers so clients know where they stand.

```python
from fastapi import FastAPI, Request, Response, Depends
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
import math

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api", rate="100/min", backend=backend)


@app.get("/items")
async def get_items(
    request: Request = Depends(throttle),
    response: Response = None,
):
    stat = await throttle.stat(request)
    if stat:
        response.headers["X-RateLimit-Limit"] = str(int(stat.rate.limit))
        response.headers["X-RateLimit-Remaining"] = str(max(int(stat.hits_remaining), 0))
        if stat.wait_ms > 0:
            retry_after = math.ceil(stat.wait_ms / 1000)
            response.headers["Retry-After"] = str(retry_after)

    return {"items": [...]}
```

!!! tip "Built-in header support"
    You can also configure headers directly on the throttle using the `headers` parameter and the `Header` API — Traffik will resolve them automatically on each throttled response. See the [Headers reference](../core-concepts/index.md) for details.

---

## Building a /usage Endpoint

Give your clients a dedicated endpoint to check their quota without making a real request:

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

api_throttle = HTTPThrottle("api:standard", rate="1000/hour", backend=backend)


@app.get("/usage")
async def get_usage(request: Request):
    """Returns quota info without consuming quota."""
    stat = await api_throttle.stat(request)

    if stat is None:
        return {"message": "No rate limit info available (you may be exempt)"}

    return {
        "limit": int(stat.rate.limit),
        "remaining": max(int(stat.hits_remaining), 0),
        "reset_in_ms": stat.wait_ms,
        "window": str(stat.rate),
    }
```

Clients can hit `/usage` as often as they like — it reads the counter but never writes to it.

---

## Prometheus Metrics Example

If you're exporting metrics to Prometheus, stats give you everything you need:

```python
from prometheus_client import Gauge, Counter
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api", rate="1000/hour", backend=backend)

quota_remaining = Gauge(
    "traffik_quota_remaining",
    "Quota remaining for the current window",
    ["throttle_uid"],
)
throttle_hits = Counter(
    "traffik_requests_total",
    "Total requests processed by throttle",
    ["throttle_uid"],
)


@app.middleware("http")
async def collect_throttle_metrics(request: Request, call_next):
    response = await call_next(request)

    stat = await throttle.stat(request)
    if stat:
        quota_remaining.labels(throttle_uid="api").set(stat.hits_remaining)
        throttle_hits.labels(throttle_uid="api").inc()

    return response
```

!!! warning "Avoid calling stat() in tight loops"
    Each `stat()` call hits the backend. In a middleware that runs on every request, that's fine — it's one extra read per request. But calling it in a loop for many keys at once is a different story. Batch them if you need to.

---

## Summary

- `stat()` is your window into the rate limiter's current state — **without modifying it**
- Returns `None` for exempt clients and strategies without stat support
- Use it for response headers, usage endpoints, dashboards, and metrics
- All built-in strategies support `stat()` with typed metadata
