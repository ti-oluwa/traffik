# Traffik

<div class="hero" markdown>

## Stop the flood. Let the right traffic through

**Traffik** is async-first, distributed rate limiting for FastAPI and Starlette, built for correctness under pressure, not just greenfield demos.

[Get Started](installation.md){ .md-button .md-button--primary }
[Quick Start](quickstart.md){ .md-button }

</div>

---

## What is Traffik?

Rate limiting is one of those things that looks simple until it isn't. A naive counter in a dictionary works fine locally, then falls apart the moment you add a second server, a Redis timeout, or a burst of 5,000 simultaneous requests.

Traffik is built to handle all of that, correctly. It's an async, distributed rate limiter for FastAPI and Starlette that gives you atomic operations, 10+ proven strategies, HTTP *and* WebSocket support, circuit breakers, and backend failover, all with an API that stays out of your way.

Zero ceremony. Maximum protection.

---

## Features at a Glance

<div class="grid cards" markdown>

- :zap: **Fully Async**

    ---

    Built for `async`/`await` from the ground up. Non-blocking by default, with minimal latency overhead on the hot path.

- :earth_africa: **Distributed-First**

    ---

    Atomic operations with distributed locks (Redis, Memcached). Correct counts even under high concurrency across multiple app instances.

- :chart_with_upwards_trend: **10+ Strategies**

    ---

    Fixed Window, Sliding Window (Log & Counter), Token Bucket, Leaky Bucket, GCRA, Adaptive, Tiered, Priority Queue, and more.

- :handshake: **HTTP & WebSocket**

    ---

    Full-featured throttling for both protocols. Per-connection throttling on HTTP, per-message throttling on WebSocket.

- :shield: **Production-Ready**

    ---

    Circuit breakers, automatic retries, backend failover. When Redis goes down, your API stays up.

- :electric_plug: **Flexible Integration**

    ---

    Dependencies, decorators, middleware, or direct calls. Use whatever fits your architecture.

- :wrench: **Highly Extensible**

    ---

    Clean, well-documented APIs for custom backends, strategies, identifiers, and error handlers.

- :bar_chart: **Observable**

    ---

    Rich error context, strategy statistics, and quota introspection for monitoring and debugging.

- :racing_car: **Performance-Optimized**

    ---

    Lock striping, smart short-circuiting, script caching, and a minimal memory footprint.

</div>

---

## A Quick Look

Add rate limiting to a FastAPI endpoint in five lines:

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")  # (1)!
app = FastAPI(lifespan=backend.lifespan)       # (2)!

throttle = HTTPThrottle("api:items", rate="100/min")  # (3)!

@app.get("/items", dependencies=[Depends(throttle)])  # (4)!
async def list_items():
    return {"items": ["widget", "gizmo", "thingamajig"]}
```

1. Start with the in-memory backend for local development — no external services needed.
2. Pass the backend's lifespan to FastAPI so it initializes and cleans up properly.
3. Define your throttle with a unique ID and a human-readable rate string.
4. Attach it as a FastAPI dependency. That's it. Exceeded limits return `HTTP 429` automatically.

That's it. No middleware to wire up, no decorators to memorize, no configuration files to manage. When you're ready to go distributed, swap `InMemoryBackend` for `RedisBackend` and you're done.

---

## Why Traffik?

There are other rate limiting libraries out there. Here's what makes Traffik different.

### Correctness under concurrency

Simple in-process rate limiters use a plain Python dict. That works until two requests hit simultaneously and both read the same counter before either writes back. Traffik uses atomic operations at the backend level, Lua scripts for Redis, distributed locks for Memcached, so your counts are always correct, even at high concurrency.

### Async all the way down

Traffik is not a sync library with an `async` wrapper bolted on. Every backend operation, identifier resolution, and error handler is a coroutine. No `run_in_executor`, no thread pools, no hidden blocking calls.

### WebSocket support that actually works

Most rate limiters only think about HTTP. Traffik ships a `WebSocketThrottle` that handles both connection-level throttling (gate the handshake) and message-level throttling (rate-limit individual frames). By default, when a WebSocket client sends too many messages, Traffik sends them a structured JSON notification rather than slamming the connection shut.

### Fail gracefully

Backend unavailable? You choose what happens: allow the request through (`on_error="allow"`), treat it as throttled (`on_error="throttle"`), raise the error (`on_error="raise"`), or use a full circuit breaker with automatic failover to a secondary backend. No silent data loss, no surprise outages.

### Identifiers as a natively supported concept

Who counts as "one client"? An IP address? A user ID? An API key? A tenant? In Traffik, this is a natively supported concept. Pass any async function as `identifier` and Traffik uses it everywhere. Returning the special `EXEMPTED` sentinel from your identifier function bypasses rate limiting entirely for that connection, no special cases needed.

### Plays nicely with FastAPI

Throttles are callable objects that implement `__call__` with a clean, FastAPI-compatible signature. They work as `Depends(...)` arguments, `@throttled(...)` decorators, or direct `await throttle(request)` calls. FastAPI's OpenAPI schema generation never sees the throttle parameters.

---

## Backends at a Glance

| Backend | Best For | Distributed | Persistence |
|---|---|---|---|
| `InMemoryBackend` | Development, testing, single-process | No | No |
| `RedisBackend` | Production, multi-instance deployments | Yes | Yes |
| `MemcachedBackend` | High-throughput, cache-friendly workloads | Yes | Best-effort |

---

## Ready to go?

<div class="grid cards" markdown>

- :rocket: **Installation**

    ---

    Get Traffik installed and pick your backend.

    [Installation](installation.md){ .md-button }

- :books: **Quick Start**

    ---

    From zero to protected endpoint in minutes. With a real production setup at the end.

    [Quick Start](quickstart.md){ .md-button }

- :bulb: **Core Concepts**

    ---

    Understand rates, backends, strategies, and identifiers before diving into advanced features.

    [Core Concepts](core-concepts/index.md){ .md-button }

</div>


