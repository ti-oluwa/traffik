<p align="center">
  <img src="docs/assets/logo.svg" alt="Traffik Logo" width="150">
</p>

<h1 align="center">Traffik</h1>

<h3 align="center">
  <strong>Rate limiting for Starlette applications</strong>
</h3>

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Traffik is a rate limiting library for Starlette and FastAPI. Write the throttle once, point it at whatever storage you want, and it just works. You can use the in-memory backend while developing, Redis or Memcached once you need to share state across processes (probably in production).

This README covers enough to get productive fast. Everything else is detailed in the **[full documentation](https://ti-oluwa.github.io/traffik/)**.

```bash
pip install traffik
```

That's all you need for the in-memory backend. For Redis or Memcached, see [Backends](#backends).

## Quickstart

Every client, identified by IP by default, gets a maximum of 100 requests per minute to `/items`:

```python
# main.py
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:items", rate="100/min")

@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": []}
```

> `uid` is a unique id for the throttle. It is used for namespacing entries in the backend and some advanced features covered in the docs.

Run with `uvicorn main:app`, hit `/items` over 100 times in a minute, get a `429`. That's the whole contract; everything else is configuration on top of it.

## How Traffik Works

Three replaceable pieces, each configurable on its own:

- **`Throttle`** - This is what you attach to a route. Holds the rate, cost, identifier, error policy, and backend. `HTTPThrottle` for HTTP, `WebSocketThrottle` for WebSocket connections/messages.
- **Strategy** - THis part decides *how* the limit is enforced (`FixedWindow`, `SlidingWindow`, `TokenBucket`, `GCRA`, and more). Given a key and a rate, it says whether to let the request through and how long to wait if not.
- **Backend** - Here is where the counters actually live: `InMemoryBackend`, `RedisBackend`, `MemcachedBackend`, or the experimental `MultiProcessInMemoryBackend` for sharing state across workers on one machine without Redis.

Backends expose a `.lifespan` you pass to FastAPI (as above) for automatic setup/teardown; you can also manage that manually, or use a backend as a context manager directly. Full breakdown: [Core Concepts](https://ti-oluwa.github.io/traffik/core-concepts/).

## Backends

| Backend | Needs | Use case |
| --- | --- | --- |
| `InMemoryBackend` | nothing | dev, tests, single worker |
| `MultiProcessInMemoryBackend` *(experimental)* | nothing (stdlib) | multiple workers, one machine, no Redis |
| `RedisBackend` (`redis.asyncio` or `coredis`) | `traffik[aioredis]` / `traffik[coredis]` | production, distributed |
| `MemcachedBackend` (`aiomcache` or `emcache`) | `traffik[aiomcache]` / `traffik[emcache]` | existing Memcached stacks |

```python
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)
```

`InMemoryBackend` state lives in-process which is fine for one worker, but silently gets wrong the moment you run more than one (limits multiply by worker count). Everything else, client comparisons, cluster/Sentinel setup, the multi-process backend's constraints, dependency install matrix, is in **[Backends](https://ti-oluwa.github.io/traffik/core-concepts/backends/)**.

## Strategies

```python
from traffik.strategies import FixedWindow, SlidingWindowCounter, TokenBucket, GCRA
# + SlidingWindowLog, TokenBucketWithDebt, LeakyBucket, LeakyBucketWithQueue

throttle = HTTPThrottle("api", rate="100/min", strategy=SlidingWindowCounter())
```

Traffik defaults to `FixedWindow` which is the cheapest, and is correct for most APIs, and the only one that never needs a lock. Use `SlidingWindowCounter` if boundary bursts matter, `TokenBucket` for controlled bursts, `GCRA` for perfectly even spacing. Six more custom strategies (`TieredRateStrategy`, `AdaptiveThrottleStrategy`, and others solving narrower problems) live in `traffik.strategies.custom`. Details and examples for all of them: **[Strategies](https://ti-oluwa.github.io/traffik/core-concepts/strategies/)**.

## Rate Formats

```python
"100/min"       # 100 per minute
"10/30s"        # 10 per 30 seconds
"200/500ms"     # sub-second windows
Rate(limit=50, minutes=1)  # explicit object
```

Full grammar: **[Rates](https://ti-oluwa.github.io/traffik/core-concepts/rates/)**.

## Integration Patterns

Throttles work as FastAPI dependencies (shown above, also on routers via `APIRouter(dependencies=[...])`), as decorators (`@throttled(...)` - has different import paths for Starlette vs. FastAPI), as blanket middleware rules across routes (`ThrottleMiddleware` + `Throttle`(s)), or called directly (`await throttle.hit(...)`) anywhere in your code, including per-message inside a WebSocket loop. See **[Integration Patterns](https://ti-oluwa.github.io/traffik/integration/)** for a worked example of each.

## Also Included

Briefly, since these all have worked examples in the docs:

- **[Custom identifiers](https://ti-oluwa.github.io/traffik/core-concepts/identifiers/)** - You can key on API key, user ID, tenant, whatever you want instead of IP; return `EXEMPTED` to let specific connections through unconditionally.
- **[Cost-based throttling](https://ti-oluwa.github.io/traffik/advanced/request-costs/)** - You can specify that expensive endpoints consume more than 1 hit per request.
- **[Response headers](https://ti-oluwa.github.io/traffik/advanced/headers/)** - `X-RateLimit-*` / `Retry-After` / any custom header, declarative or resolved manually.
- **[Rules](https://ti-oluwa.github.io/traffik/advanced/rules/)** - Gate when a throttle applies or is bypassed, based on method, predicate, etc.
- **[Deferred quota](https://ti-oluwa.github.io/traffik/advanced/quota-context/)** (`QuotaContext`) - Only consumes quota if an operation actually succeeds; batch several throttles into one "transaction".
- **[Error handling and resilience](https://ti-oluwa.github.io/traffik/error-handling/)** - Fail open/closed, automatic failover to a secondary backend with a circuit breaker, retry policies, etc.
- **[Dynamic backends](https://ti-oluwa.github.io/traffik/advanced/context-backends/)** - Route different requests/tenants to different backends at runtime.
- **Runtime updates** - `await throttle.update_rate(...)`, `.disable()` / `.enable()`, or globally via `GLOBAL_REGISTRY`. See **[Registry](https://ti-oluwa.github.io/traffik/advanced/registry/)**.
- **[Testing state without consuming it](https://ti-oluwa.github.io/traffik/advanced/statistics/)** - `await throttle.stat(request)` / `.check(...)`.

## Performance

Two independent choices set your overhead, the **backend** (baseline cost - in-memory is sub-millisecond, Redis/Memcached are dominated by their round trip, the multi-process backend hops through a thread pool) and the **strategy** (whether you pay a locking tax on top - `FixedWindow`/`GCRA` never lock, anything that reads-computes-writes state has to, for correctness). The multi-process backend in particular can cost more than plain Redis under hot-key, lock-heavy loads - worth reading before you pick it over a "real" distributed backend. Full breakdown, including a case where local Redis beats the multi-process backend and why: **[Performance](https://ti-oluwa.github.io/traffik/performance/)**.

A benchmark suite ships with the repo for testing this against your own traffic shape, and includes a head-to-head comparison against SlowAPI:

```bash
make install-bench                          # pulls in the benchmark-only deps
make bench http                             # defaults: in-memory, fixed window
make bench "http --backend aioredis --strategy token_bucket"
make bench "compare --backend inmemory"     # vs. SlowAPI, matched conditions
```

Commands, options, and how to read the output: **[Benchmarks](https://ti-oluwa.github.io/traffik/benchmarks/)**.

## Full Documentation

**[https://ti-oluwa.github.io/traffik/](https://ti-oluwa.github.io/traffik/)** - For everything above, in depth, plus the full API reference.

## Contributing

Issues and PRs welcome. `make dev-setup` for a working dev environment, `make test-fast` before you push, `make quality` before you open a PR. See `CONTRIBUTING.md` for details.

If you find this useful, a star helps others find it.

## License

MIT
