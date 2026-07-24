# Traffik

<div class="hero" markdown>

**Traffik** is async-first, distributed rate limiting for Starlette and FastAPI applications.

[Get Started](installation.md){ .md-button .md-button--primary }
[Quick Start](quickstart.md){ .md-button }

</div>

---

## What is Traffik?

Rate limiting is an essential part of preventing abuse of your application's resources or endpoints. It allows you to specify access limits usually over a time window. The simplest implementation uses a counter and timestamp stored in a dictionary held in memory. This works fine locally on a single worker application, but falls apart the moment you add a second worker, or you need it to work across multiple instances of your application on different machines (servers). Then comes the question, "How do we reliably enforce API resource access limit for clients across multiple instances of our application, possibly hosted on servers spread out across different locations?"

Traffik started as "I need to rate limit a multi-instance API deployment" and grew into a fairly complete (maybe over-engineered) toolkit for it. The core API contains a handful of strategies, a couple of backends, which covers what you'll mostly need. There's a lot more underneath if you want it; 14 strategies, HTTP and WebSocket support, robust error handling policies, dynamic or context aware backend usage, deferred quota, etc.

## How to Install

```bash
pip install traffik
```

That's all you need to get started - atleast for a single worker application. For backends that support multi-instance setups, see [Backends](core-concepts/backends.md).

---

## A Quick Look

Here we have a simple FastAPI endpoint that returns a list of items, and we need ensure that all clients can fetch this list for at most 100 times per minute.

```python
# main.py
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")   # (1)!
app = FastAPI(lifespan=backend.lifespan)       # (2)!

throttle = HTTPThrottle(uid="api:items", rate="100/min")   # (3)!

@app.get("/items", dependencies=[Depends(throttle)])       # (4)!
async def list_items():
    return {"items": ["widget", "gizmo", "thingamajig"]}
```

1. Start with the in-memory backend for local development. No external services needed.
2. Pass the backend's `lifespan` to FastAPI so it initializes and cleans up properly on application startup/shutdown.
3. `uid` is a required, unique ID. It's how Traffik namespaces this throttle's state in the backend, separately from every other throttle sharing that backend.
4. Attach it as a dependency. Exceeding the limit returns `HTTP 429` automatically.

Run it with `uvicorn main:app`, hit `/items` more than 100 times in a minute, and you'll get a 429 right after the 100th request. That's essentially the whole contract. Everything else in these docs is configuration on top of it.

---

## How Traffik works

A request hits three things on its way through a throttled route, each swappable on its own:

- **The `Throttle`:** The thing you attach to a route or endpoint. Holds the rate (or a rate function), the cost (or cost function), the request/client identifier, the error policy, and an optional preferred backend, plus a few other knobs. You get two kinds: `HTTPThrottle` for regular routes, `WebSocketThrottle` for websocket connections (the initial handshake) and individual messages/frames.

- **The Strategy:** The algorithm that decides whether a given request should be served. It reads the current state for an identified request from the backend, runs its check, decides "serve it" or "needs to wait `N`ms", and writes the new state back to the backend. Strategies available include: `FixedWindow`, `TokenBucket`, `LeakyBucket`, `GCRA`, and ten others - see [Strategies](core-concepts/strategies.md) for more on strategies.

- **The Backend:** Where the counters and records actually live. Traffik implements three main kinds - `InMemoryBackend`, `RedisBackend`, `MemcachedBackend`, and then an experimental `MultiProcessInMemoryBackend` for sharing state across workers on one machine without standing up a distributed backend like `RedisBackend`. The strategy reads from and writes to it.

Backends expose a `lifespan` context manager, which is what you pass to FastAPI in the example above. It's not strictly required. You can manage a backend's lifecycle manually if your setup needs that, but it's the common case. The lifespan initializes the backend on application startup and tears it down (cleans up backend connections and resources) on shutdown. Backends themselves can also be used as plain async context managers, so you can use one directly in middleware, inside an endpoint, or from a service layer, without going through `lifespan` at all.

---

## Backends at a Glance

| Backend | Best for | Distributed | Dependencies |
|---|---|---|---|
| `InMemoryBackend` | Development, single-process deployments | No | None |
| `MultiProcessInMemoryBackend` | Multiple workers, one machine, no Redis | Same machine only | None (stdlib `multiprocessing`) |
| `RedisBackend` | Production, multi-instance deployments | Yes | `redis.asyncio` or `coredis` |
| `MemcachedBackend` | High-throughput, cache-friendly workloads | Yes | `aiomcache` or `emcache` |

!!! warning "`MultiProcessInMemoryBackend` is experimental"
    It works, and it's tested, but the setup constraints are real: it needs the `fork` start method (Linux, or macOS with it set explicitly), and there's exactly one correct way to stand it up - construct and `start()` it once in a parent process *before* forking, and let every worker inherit it through the fork. See [Backends](core-concepts/backends.md#multiprocessinmemorybackend) before reaching for this one.

---

## Why Traffik?

**Correctness under concurrency.**

A naive in-process limiter reads a counter, checks it, writes it back. Two concurrent requests can both read "3 left" before either writes, which quietly breaks your limit. Traffik's strategies that need read-check-write (`TokenBucket` and friends) hold a lock across all three steps. Some algorithms like `FixedWindow` and `GCRA` skip the lock entirely - they do one atomic increment, so no race to worry about.

**Fully Asynchronous API.**

Every backend operation, identifier resolution, and error handler is a coroutine. No `run_in_executor` bolted on top of something sync (the multiprocess backend is the one exception - see [Performance](performance.md)).

**Solid WebSockets support.**

`WebSocketThrottle` allows you enforce both connection-level gating (reject the handshake) and per-message throttling. When a client sends too many messages, Traffik sends a structured JSON notification by default instead of just killing the connection.

**Robust and custome error handling.**

Is your backend down because of a transient Redis error? You can choose how that scenario is handle. You can decide to let requests through (`on_error="allow"`), treat them as throttled (`"throttle"`, the default), raise it yourself (`"raise"`), wire up a circuit breaker with automatic failover to a secondary backend, or setup a custom failure recovery policy.

**Identifiers are just async functions.**

"A client" can mean an IP, a user ID, an API key, a tenant. So pass any async callable as an `identifier` and it's used everywhere that throttle is. You can return the `EXEMPTED` sentinel to bypass throttling entirely for a given connection, as simple as that.

**Dependency injection friendly.**

Although throttles are designed to be awaited explicitly in the endpoint, they are also callables with a FastAPI-compatible `__call__` signature. Use them as `Depends(...)`, `@throttled(...)` decorators, or just `await throttle(request)` directly. FastAPI's OpenAPI schema never sees throttle-internal parameters.

---

## Ready to go?

<div class="grid cards" markdown>

- :rocket: **Installation**

    ---

    Get Traffik installed and pick your backend.

    [Installation](installation.md){ .md-button }

- :books: **Quick Start**

    ---

    From zero to a throttled endpoint, with a production-ready example at the end.

    [Quick Start](quickstart.md){ .md-button }

- :bulb: **Core Concepts**

    ---

    Rates, backends, strategies, and identifiers, before you get into the advanced stuff.

    [Core Concepts](core-concepts/index.md){ .md-button }

</div>
