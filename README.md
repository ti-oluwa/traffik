# Traffik

Rate limiting for FastAPI and Starlette. Write the throttle once, point it at whatever's
storing your counters - in-memory while you're developing, Redis or Memcached once you
actually need to share state across processes - without rewriting the throttle itself.

This started as "I need to rate limit an API" and grew into a fairly complete toolkit for it.
The core (fixed window, sliding window, token bucket, a couple of backends) covers what
most people need. There's more under the hood if you want it - multi-process shared
memory so you don't need Redis just to keep two workers honest, GCRA if you care about
perfectly even request spacing, eight extra strategies for weirder problems. You don't
need to know any of that exists to use the basics below.

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

```bash
pip install traffik
```

That's all that's need for use with an in-memory backend - no extra dependencies. For Redis or Memcached, see
[Backends](#backends) below.

## Quickstart

```python
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

Run it, hit `/items` more than 100 times in a minute, get a `429`. That's the whole
contract. Everything below is configuration on top of this.

## How it fits together

Three pieces, each swappable on its own:

- **`Throttle`** - the thing you attach to a route. Holds the rate, the cost function,
  the identifier function, the error policy.
- **Strategy** - the algorithm (`FixedWindow`, `TokenBucket`, `GCRA`, ...). Decides, given
  a key and a rate, whether to let a request through.
- **Backend** - where counters actually live (`InMemoryBackend`, `RedisBackend`, ...). The
  strategy reads and writes through it and has no idea what's on the other side.

This is the whole point of the library, really: a throttle built against
`InMemoryBackend` in dev and one built against `RedisBackend` in prod behave identically
from the call site. You change one line, not your route logic.

## Backends

### In-memory - single process, no dependencies

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)
```

State lives in the process. Fine for local dev and single-worker deployments. Wrong
choice the moment you run more than one worker - each worker would count independently
and your real limit becomes `configured_limit × worker_count`.

### Multi-process shared memory - multiple workers, one machine

If you're running gunicorn/uvicorn with several workers on a single box and don't want
to stand up Redis just to get accurate counts across them:

```python
from traffik.backends.multiprocess import MultiProcessInMemoryBackend

# Create before forking - in your app factory or gunicorn's `on_starting`.
backend = MultiProcessInMemoryBackend.create(
    namespace="myapp",
    max_keys=65536,
    number_of_shards=64,     # rule of thumb: 2 × worker count
    cleanup_frequency=30.0,  # reclaim expired slots periodically
)
app = FastAPI(lifespan=backend.lifespan)
```

Workers attach to the same POSIX shared-memory segment after `fork`. No network hop,
counters stay accurate across all of them. Requires Linux, or macOS with
`multiprocessing.set_start_method("fork")` set explicitly (`fork` is already the default
on Linux).

In a worker that needs to attach to a segment created elsewhere:

```python
backend = MultiProcessInMemoryBackend.attach(namespace="myapp")
```

**Where it loses to Redis, honestly:** this backend shards keys across `number_of_shards`
and locks per-shard. One very hot key (a single IP hammering one endpoint) always lands
on the same shard, so concurrent requests against *that one key* queue up on the shard's
lock - and under real load that shows up as a latency cliff, not a graceful slowdown.
Spread keyspaces (lots of distinct users/IPs) barely notice this, and there it's close to
in-memory and ahead of Redis. But Redis has no client-side lock at all for a plain
`INCR` - it just doesn't have this problem, ever, on any keyspace shape. If your traffic
is one endpoint getting hammered by a small number of clients, measure both before
reaching for multiprocess over Redis. Don't take my word for the shape of that trade-off
either - run the benchmarks in `benchmarks/` against something that looks like your
actual traffic.

### Redis - `redis.asyncio`

```python
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
```

### Redis - `coredis` (clusters, Sentinel)

```python
from traffik.backends.redis.coredis import RedisBackend

# Single node
backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")

# Cluster
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

Default to `aioredis` (`redis.asyncio`) unless you specifically need cluster or Sentinel
support - it's the more mature client and measurably faster in our benchmarks (see
`benchmarks/`). Reach for `coredis` only when you need what it offers that `aioredis`
doesn't.

### Memcached - `aiomcache`

```python
from traffik.backends.memcached.aiomcache import MemcachedBackend

backend = MemcachedBackend(host="localhost", port=11211, namespace="myapp")
```

### Memcached - `emcache` (Linux/macOS, multi-node)

```python
from traffik.backends.memcached.emcache import MemcachedBackend

backend = MemcachedBackend(
    nodes=["memcached://node1:11211", "memcached://node2:11211"],
    namespace="myapp",
)
```

Rendezvous hashing across nodes, adaptive connection pool. Faster than `aiomcache` at
high throughput, Linux/macOS only.

```bash
pip install "traffik[aioredis]"    # redis.asyncio
pip install "traffik[coredis]"     # coredis - clusters, Sentinel
pip install "traffik[aiomcache]"   # memcached via aiomcache
pip install "traffik[emcache]"     # memcached via emcache, Linux/macOS only
pip install "traffik[all]"         # everything
```

> `traffik[redis]` and `traffik[memcached]` still work as aliases for `aioredis` and
> `aiomcache` respectively, kept for backward compatibility with 1.1.x.

## Strategies

```python
from traffik.strategies import (
    FixedWindow,           # simple, cheap, allows boundary bursts up to 2x
    SlidingWindowCounter,  # weighted two-window approximation, good default
    SlidingWindowLog,      # exact, more memory
    TokenBucket,           # allows controlled bursts
    TokenBucketWithDebt,   # token bucket with configurable overdraft
    LeakyBucket,           # smooths output, no bursts
    LeakyBucketWithQueue,  # strict FIFO ordering
    GCRA,                  # perfectly smooth, zero burst tolerance by default
)

throttle = HTTPThrottle("api", rate="100/min", strategy=SlidingWindowCounter())
```

Skip the argument and you get `FixedWindow` - cheapest one, correct for most APIs, and
it's the default for a reason: it's the only strategy that never needs a lock, so it's
fast on every backend, including the ones where locking gets expensive (see
[Performance](#performance)). Reach for `SlidingWindowCounter` if boundary bursts
actually bite you in practice, `TokenBucket` if you want to allow short bursts on top of
a sustained rate, `GCRA` if you need genuinely even request spacing (think telecom-style
SLAs, not "please don't hammer my API").

### Advanced strategies

`traffik.strategies.custom` has eight more: `TieredRateStrategy`,
`AdaptiveThrottleStrategy`, `PriorityQueueStrategy`, `QuotaWithRolloverStrategy`,
`TimeOfDayStrategy`, `CostBasedTokenBucketStrategy`, `DistributedFairnessStrategy`,
`GeographicDistributionStrategy`. Honest framing: these exist because the problems were
interesting to solve, not because most APIs need them. Per-tier limits, load-adaptive
throttling, multi-instance fairness, region-aware quotas - real problems, just not
*your* problem most of the time. Each has its own docs and example in the
[full documentation](https://ti-oluwa.github.io/traffik/) if one of these genuinely
matches something you're dealing with.

One honest caveat on two of them: `DistributedFairnessStrategy` and
`GeographicDistributionStrategy` are solving problems an API gateway (Envoy, Kong, your
cloud LB) is usually a better fit for than an in-process Python library. If you're
reaching for either of these, it's worth asking whether the right fix is one layer down
the stack instead.

## Rate formats

```python
"100/min"      # 100 per minute
"5/s"          # 5 per second
"10/30s"       # 10 per 30 seconds
"1000/hour"
"500/day"
"200/500ms"    # sub-second windows

Rate(limit=50, minutes=1)   # explicit Rate object
```

## Integration patterns

### FastAPI dependency

```python
@app.get("/search", dependencies=[Depends(throttle)])
async def search():
    ...
```

### FastAPI decorator (keeps the OpenAPI schema clean)

Use this when you don't want the throttle showing up as a route parameter in your docs.

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

`path` and `methods` go on `MiddlewareThrottle`, not on `ThrottleMiddleware` itself -
`ThrottleMiddleware` just runs whichever `MiddlewareThrottle`s match a given request.

### WebSockets

```python
from traffik.throttles import WebSocketThrottle, is_throttled

ws_throttle = WebSocketThrottle("ws:messages", rate="30/min")

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await ws_throttle.hit(websocket, context={"scope": "message"})
        if is_throttled(websocket):
            await websocket.send_text("Throttled! Please wait before sending more messages.")
            continue
        await websocket.send_text(process(data))
```

## Custom identifiers

Default identifier is the client IP. Key on whatever you actually want - user ID, API
key, tenant ID:

```python
async def api_key(request: Request) -> str:
    return request.headers.get("X-API-Key") or request.client.host

throttle = HTTPThrottle("api", rate="100/min", identifier=api_key)
```

Return `EXEMPTED` to let specific connections through unconditionally:

```python
from traffik.types import EXEMPTED

async def identifier(request: Request):
    if request.headers.get("X-Internal-Token") == SECRET:
        return EXEMPTED
    return request.client.host
```

## Cost-based throttling

Not all requests should count the same:

```python
async def request_cost(request: Request, context=None) -> int:
    if "/export" in request.url.path:
        return 10
    return 1

throttle = HTTPThrottle("api", rate="100/min", cost=request_cost)

# or override per-call
await throttle.hit(request, cost=5)
```

## Response headers

```python
from traffik.headers import Headers, Header

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    headers=Headers({
        "X-RateLimit-Limit": Header.LIMIT(when="always"),
        "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        "Retry-After": Header.RESET_SECONDS(when="throttled"),
    }),
)
```

To resolve headers manually, e.g. inside a custom throttled handler:

```python
from traffik.exceptions import ConnectionThrottled

async def my_handler(request, wait_ms, throttle, context):
    headers = await throttle.get_headers(request, context=context)
    raise ConnectionThrottled(wait_period=int(wait_ms / 1000), headers=headers)
```

## Rules and bypass

Gate when a throttle applies, or skip it for certain traffic:

```python
from traffik.registry import ThrottleRule, BypassThrottleRule

# Only apply the global throttle to write methods
write_throttle.add_rules(
    "api:global",
    ThrottleRule(methods={"POST", "PUT", "PATCH", "DELETE"}),
)

# Skip throttling for internal traffic
async def is_internal(request: Request) -> bool:
    return request.headers.get("X-Internal") == "1"

throttle = HTTPThrottle(
    "api:global",
    rate="100/min",
    rules=[BypassThrottleRule(predicate=is_internal)],
)
```

## Deferred quota (`QuotaContext`)

Only consume quota if an operation actually succeeds, or consume several throttles
atomically as one unit:

```python
async def create_order(request: Request):
    async with order_throttle.quota(request, lock=True) as quota:
        quota(cost=1)                       # this throttle
        quota(heavy_ops_throttle, cost=5)   # a different throttle, same transaction

        result = await do_expensive_work()
    # quota only consumed here, on clean exit
    return result
```

Consecutive calls to the same throttle with the same config aggregate into one backend
operation - `quota(cost=2); quota(cost=3)` becomes a single `increment(key, 5)`.

Check available quota without consuming it:

```python
async with throttle.quota(request) as quota:
    if not await quota.check(cost=10):
        raise HTTPException(429, "Insufficient quota")
    quota(cost=10)
    result = await heavy_work()
```

## Error handling and resilience

What happens when the backend itself fails:

```python
throttle = HTTPThrottle("api", rate="100/min", on_error="allow")     # fail open
throttle = HTTPThrottle("api", rate="100/min", on_error="throttle")  # fail closed (default)
throttle = HTTPThrottle("api", rate="100/min", on_error="raise")     # handle it yourself
```

Or fall back to a secondary backend with an automatic circuit breaker:

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

After 5 consecutive Redis failures, the circuit opens - new requests fall back to
in-memory immediately, no waiting on a timeout. It half-opens after 30 seconds, lets one
probe request through, and closes again on success.

For transient errors:

```python
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    on_error=retry(max_retries=3, retry_delay=0.05, retry_on=(TimeoutError,)),
)
```

## Dynamic backends (multi-tenant)

Route different tenants to different backends at runtime, without one throttle per
tenant:

```python
throttle = HTTPThrottle("api", rate="100/min", dynamic_backend=True)

@app.get("/data")
async def data(request: Request):
    tenant_backend = get_backend_for_tenant(request)
    async with tenant_backend(request.app):
        return await throttle.hit(request)
```

Adds roughly 1–20ms of backend-resolution overhead per request. Use an explicit
`backend=` instead if you don't actually need per-tenant routing.

## Runtime updates

Change a throttle's configuration without recreating it:

```python
await throttle.update_rate("200/min")
await throttle.update_strategy(TokenBucket())
await throttle.update_cost(5)
await throttle.disable()    # let everything through temporarily
await throttle.enable()
```

Or globally, via the registry:

```python
from traffik.registry import GLOBAL_REGISTRY

await GLOBAL_REGISTRY.disable_all()   # maintenance mode
await GLOBAL_REGISTRY.enable_all()
```

## Checking state without consuming it

```python
stat = await throttle.stat(request)
if stat:
    print(f"{stat.hits_remaining} hits left, {stat.wait_ms}ms until reset")
    print(stat.metadata)  # strategy-specific details

# or just a boolean
if not await throttle.check(request, cost=5):
    raise HTTPException(429, "Not enough quota for this operation")
```

## Performance

Two things determine your actual overhead, and they don't move together: which backend
you pick, and which strategy you pick.

**Backend** decides your baseline cost. In-memory has no network or IPC involved - sub-
millisecond, full stop. Redis and Memcached are dominated by the round trip to wherever
those services live, not by anything traffik itself is doing. Multi-process shared
memory skips the network entirely, but that's not a free lunch - see below.

**Strategy** decides whether you pay a locking tax on top of that baseline.
`FixedWindow` does one atomic increment and never takes a lock - cheap on every backend,
including the ones where locking is expensive. `TokenBucket` and anything else that
needs to read state, do math, and write it back *has* to hold a lock across all three
steps, or two concurrent requests can both read "3 tokens left" and both spend one,
which quietly breaks your rate limit. That's not a bug, it's what correctness costs for
that class of algorithm - but it means the backend you'd otherwise pick on gut feel can
behave differently once a real lock is involved.

This is exactly where the multi-process backend gets more expensive than you'd expect:
every read and every write has to hop through a thread pool (`run_in_executor`) because
the underlying primitives are blocking, not async. `FixedWindow` pays that tax once per
request. `TokenBucket` pays it twice - once for the read, once for the write - while
holding a per-shard lock the whole time. Stack fifty concurrent requests on one hot key
and that queues up fast. Redis, despite paying real network round trips for the same
get-lock-read-write-unlock sequence, ends up faster here, because a socket write to
localhost is cheaper than a thread-pool handoff plus an OS semaphore under contention.
Not the result I'd have guessed either, and it's a good reminder that "avoids the
network" doesn't automatically mean "faster."

None of this is something you should take on my word, or the README's word, or anyone's
word without checking. Run the benchmarks yourself, against your own traffic shape (key
cardinality, concurrency, strategy). A synthetic number from someone else's laptop is a
starting point, not a substitute for testing your own workload.

### Running the benchmarks

```bash
make install-bench   # pulls in the benchmark-only deps
make bench http                                  # everything, defaults (in-memory, fixed window)
make bench "http --backend aioredis --strategy token_bucket"
make bench "middleware --backend multiprocess"
make bench "http --scenarios hot_key,many_keys -n 5"   # just these two, 5 iterations
```

`make bench` forwards whatever you type after it straight to the CLI, so anything below
works the same way with `make bench` in front instead of `uv run -m benchmarks`.

Or skip `make` and call the CLI directly:

```bash
uv run -m benchmarks http --backend inmemory
uv run -m benchmarks http --backend aioredis --strategy token_bucket
uv run -m benchmarks middleware --backend multiprocess
uv run -m benchmarks websocket --backend inmemory
```

`--backend` is `inmemory` / `multiprocess` / `aioredis` / `coredis` / `aiomcache` /
`emcache`. `--strategy` is any of the eight core strategies from
[Strategies](#strategies) above, lowercased and snake_cased (`fixed_window`,
`token_bucket`, `gcra`, ...). Redis/Memcached backends need the corresponding service
running locally - `docker compose up -d redis memcached` handles that if you don't
already have them.

Results print as a table by default; pass `--output json` if you want to feed them into
something else. `--concurrency` controls how wide the concurrent-scenario batches are
(default 50), `-n`/`--iterations` controls how many timed runs you get per scenario
(default 3, plus one discarded warmup run).

One thing worth knowing before you trust hot-key vs. many-keys comparisons from these
benchmarks specifically: a couple of the scenarios (`many_keys`, `concurrent`) had
confounds in how they varied concurrency and client identity that made them less
apples-to-apples than they looked. Being fixed as of this writing - if you're
benchmarking key-contention behavior specifically, check `benchmarks/scenarios/http.py`
against what's described here before drawing conclusions from the numbers.

## Full documentation

[https://ti-oluwa.github.io/traffik/](https://ti-oluwa.github.io/traffik/) - advanced
strategies, WebSocket per-message throttling, testing patterns, full API reference.
Everything that didn't fit here.

## Contributing

Issues and PRs welcome. `make dev-setup` gets you a working dev environment,
`make test-fast` for a quick sanity check before you push, `make quality` before you
open a PR. See `CONTRIBUTING.md` for the actual details.

## License

MIT
