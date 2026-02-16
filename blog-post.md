---
title: Building an async rate limiter for FastAPI that works under concurrency
published: false
tags: python, fastapi, opensource, webdev
---

Rate limiting is one of those things that looks deceptively simple. A counter in a dictionary, an if-statement, done. It works in your local dev environment. It works in your test suite. Then you deploy with Redis and multiple workers, traffic spikes, and suddenly your "100 requests per minute" limit is either letting everyone through or blocking everyone.

I ran into this enough times that I decided to build something better.

## The concurrency problem

Here's what typically goes wrong. Most rate limiters follow this pattern:

1. Read the current counter from storage
2. Check if it's under the limit
3. Increment the counter

When requests arrive sequentially, this works fine. When 50 requests hit simultaneously, they all read the same counter value at step 1, all pass the check at step 2, and all try to increment at step 3. Depending on the backend, you end up with a counter that's way off — and the limiter either lets too many through or starts rejecting everything.

This is especially bad on distributed backends like Redis or Memcached, where the round-trip between read and write gives the race condition a wider window to manifest.

## What I built

[Traffik](https://github.com/ti-oluwa/traffik) is an async, distributed rate limiter for FastAPI and Starlette. The core design principle: every operation that touches shared state must be atomic. On Redis, that means Lua scripts that increment and check in a single round-trip. On Memcached, that means distributed locks with CAS operations. On the in-memory backend, that means sharded asyncio locks.

The API stays simple:

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:items", rate="100/min")

@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": ["widget", "gizmo"]}
```

When you're ready for production, swap the backend:

```python
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
```

Everything else stays the same.

## Some numbers

I ran benchmarks comparing Traffik against SlowAPI (the most popular FastAPI rate limiter) across different backends and concurrency levels. A few highlights.

### Throughput

**InMemory backend (HTTP dependency mode):**

| Scenario | Traffik (req/s) | SlowAPI (req/s) | Difference |
|----------|-----------------|-----------------|------------|
| Low load (50 req, within limit) | 1,163 | 1,343 | -13% |
| High load (200 req, over limit) | 1,983 | 1,349 | **+47%** |
| Burst (100 req, 2x limit) | 1,509 | 1,091 | **+38%** |

On happy-path scenarios where everyone's within the limit, SlowAPI is slightly faster — it does less work (no atomic operations). Once requests start getting throttled, Traffik pulls ahead because the atomic path avoids the read-check-write overhead.

**Redis backend:**

| Scenario | Traffik (req/s) | SlowAPI (req/s) | Difference |
|----------|-----------------|-----------------|------------|
| Low load | 627 | 752 | -17% |
| High load | 1,107 | 1,081 | +2% |
| Sustained load | 1,305 | 1,215 | +7% |

Redis numbers are closer. The low load gap is the cost of atomic operations when you don't actually need them. The high/sustained load gap is where atomicity pays for itself.

### Tail latency (InMemory, high load)

| Percentile | Traffik | SlowAPI |
|------------|---------|---------|
| P50 | 0.42ms | 0.50ms |
| P95 | 0.93ms | 1.86ms |
| P99 | 1.85ms | 2.97ms |

Traffik's P99 is lower because the atomic path doesn't retry on conflict. The lock serializes, computes, and returns.

### A note on correctness benchmarks

Our benchmark suite also includes correctness tests on Redis and Memcached, where SlowAPI shows significantly worse results. But I want to be upfront: the benchmark code resets Traffik's backend state between iterations (via the backend's context manager), while SlowAPI's Redis/Memcached keys aren't cleaned between runs. With a 60-second rate window and sub-second gaps between iterations, SlowAPI's counters accumulate across runs, which inflates the difference.

The underlying race condition in non-atomic read-then-increment is real and can cause issues under high concurrency, but our current benchmark numbers exaggerate the severity. We're working on fixing the benchmark to flush state between iterations for both libraries so the comparison is fair. Until then, take the correctness numbers with that caveat in mind.

What I can say with confidence: on the InMemory backend (where both libraries start clean every iteration), both achieve perfect correctness.

## What Traffik brings to the table

Beyond the atomic operations, here's what made me feel like this was worth building:

### 7 rate limiting strategies

Different use cases need different algorithms:

- **Fixed Window** — Fastest. Lock-free for windows >= 1 second.
- **Sliding Window Counter** — Smooths out the burst-at-boundary problem with minimal overhead.
- **Sliding Window Log** — Most accurate, stores every timestamp. Higher memory cost.
- **Token Bucket** — Classic. Good for APIs with occasional bursts.
- **Token Bucket with Debt** — Allows borrowing against future capacity.
- **Leaky Bucket** — Smooths output rate. Good for upstream API proxying.
- **GCRA** — Enforces strict spacing between requests. Ideal when you need evenly-distributed traffic.

```python
from traffik.strategies import SlidingWindowCounter

throttle = HTTPThrottle(
    "api:search",
    rate="100/min",
    strategy=SlidingWindowCounter(),
)
```

### WebSocket throttling

Most rate limiters only handle HTTP. Traffik supports WebSocket connections with per-connection and per-message throttling:

```python
from traffik import WebSocketThrottle, is_throttled

connection_throttle = WebSocketThrottle("ws:connect", rate="10/min")
message_throttle = WebSocketThrottle("ws:messages", rate="60/min")

@app.websocket("/ws/chat")
async def chat(websocket: WebSocket = Depends(connection_throttle)):
    await websocket.accept()
    while True:
        data = await websocket.receive_json()
        await message_throttle(websocket)
        if is_throttled(websocket):
            continue  # Client got a JSON rate_limit notification
        await websocket.send_json({"echo": data})
```

When a message is throttled, the client receives a structured JSON notification instead of the connection getting killed. The connection stays alive. WebSocket checks run sub-millisecond — 0.23ms at P50, under 2ms at P99 even with 10 concurrent connections.

### Circuit breaker + failover

Redis goes down. What happens to your rate limiter? Traffik gives you explicit control:

```python
from traffik.error_handlers import failover, CircuitBreaker
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend

redis_backend = RedisBackend("redis://localhost:6379", namespace="myapp")
fallback = InMemoryBackend(namespace="myapp:fallback")

breaker = CircuitBreaker(failure_threshold=10, recovery_timeout=60.0)

throttle = HTTPThrottle(
    "api:data",
    rate="1000/hour",
    backend=redis_backend,
    on_error=failover(backend=fallback, breaker=breaker),
)
```

When Redis fails, the circuit breaker opens and traffic routes to the in-memory fallback. When Redis recovers, it switches back automatically.

### Custom identifiers with exemptions

Who counts as "one client"? IP? User ID? API key?

```python
from traffik import HTTPThrottle, EXEMPTED

async def identify(request):
    api_key = request.headers.get("x-api-key", "")
    if api_key.startswith("internal-"):
        return EXEMPTED  # Skip rate limiting entirely
    return f"key:{api_key}"

throttle = HTTPThrottle("api:v1", rate="500/hour", identifier=identify)
```

Returning `EXEMPTED` bypasses throttling completely for that request. No special cases needed.

### Middleware mode

Don't want to modify route handlers? Use middleware:

```python
from traffik.throttles import MiddlewareThrottle
from traffik.middleware import ThrottleMiddleware

throttles = [
    MiddlewareThrottle("api", rate="100/min", path="/api/**"),
    MiddlewareThrottle("auth", rate="5/min", path="/auth/login"),
]

app.add_middleware(ThrottleMiddleware, throttles=throttles)
```

Paths support wildcards (`*` for single segment, `**` for multiple). Unmatched paths pass through with zero overhead.

### Readable rate strings

Small thing, but rates should feel natural to write:

```python
HTTPThrottle("a", rate="100/minute")
HTTPThrottle("b", rate="5/10seconds")
HTTPThrottle("c", rate="20 per 2 mins")
HTTPThrottle("d", rate="1000/500ms")
```

## What it's not

Traffik is not an API gateway. It doesn't do authentication, routing, or caching. It does one thing: rate limiting. It does that thing correctly under concurrency, across distributed backends, with sub-millisecond overhead.

It's also not a drop-in replacement for SlowAPI. The API is different. If SlowAPI works for your use case — single process, in-memory — there's no reason to switch. But if you're running distributed with Redis or Memcached, or if you need WebSocket support, multiple strategies, circuit breakers, or middleware-level throttling, Traffik was built for that.

## Getting started

```bash
pip install traffik                    # Core (InMemory only)
pip install "traffik[redis]"           # + Redis backend
pip install "traffik[memcached]"       # + Memcached backend
pip install "traffik[all]"             # Everything
```

- Docs: [ti-oluwa.github.io/traffik](https://ti-oluwa.github.io/traffik/)
- GitHub: [github.com/ti-oluwa/traffik](https://github.com/ti-oluwa/traffik)
- PyPI: [pypi.org/project/traffik](https://pypi.org/project/traffik/)

If you run into bugs or have ideas, open an issue. And if Traffik helps you, a star on GitHub would be appreciated.
