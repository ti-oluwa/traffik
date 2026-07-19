# Traffik

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Traffik is a rate limiting library for Starlette applications. With Traffik, you can write the throttle/limit once,
point it at whatever you want to use as storage and it just works. Traffik also support dependency injection in FastAPI.

By default, throttles use the in-memory storage - which you can keep while you're developing. Switch to Redis or Memcached once you need to share state across processes, especially for production or live setups.

This started as a "I need to rate limit an API" project, and grew into a fairly complete (may be over-engineered) toolkit for it.

The core API (fixed window, sliding window, token bucket, a couple of backends) covers what
you'll mostly need. However, there's more options to choose from if you want it.

```bash
pip install traffik
```

That is all that's needed for use with an in-memory backend. No extra dependencies needed. For Redis or Memcached, see
[Backends](#backends) below.

## Quickstart

Here, we have a simple rate limit setup. We're enforcing that every client, identified by IP (default identifier) can make a maximum of 100 requests per minute to our API's `/items` endpoint.

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

> Notice that the throttle object required a unique id (`uid`). This helps with easy identification and clarity when reviewing limits later on, but it is mainly used by the library to namespace entries in the backend by that specific throttle and for some other advanced usages we'll see later on.

Run it with ```uvicorn main:app```, and hit `/items` more than 100 times in a minute. You'll get a `429` just after you hit the 100 requests mark. Honestly, That is the whole contract. Everything below is configuration on top of this.

## How Traffik works

A request encounters three main pieces on arriving at a throttled route or endpoint, each replaceable on its own and can be configured to taste or a specific need:

- First, the **`Throttle`**. This is the thing you attach to a route. Holds the rate (or rate function), the cost (or cost function), the identifier, the error policy, and an optional preferred backend alongside other configurations. There are two throttle types `HTTPThrottle` - for regular HTTP routes/endpoints, and `WebSocketThrottle` for websocket connections and messages/frames.

- Next we have the **Strategy** API. A strategy defines how we implement or enforce the limit specified on the throttle object. It is like a middleman that takes the limit definition from the throttle, fetches existing info about the current request been processed from the backend, does a check against its "algorithm" and returns a decision - whether to wait because the request has been limited or we can proceed to serve the request. It then stores the "decision" in the backend finally.

It mainly returns how long the client needs to wait before making its next request, that is if the request hit the limit set. The strategies included are  `FixedWindow`, `TokenBucket`, `LeakyBucket`, `GCRA`, and other unique variants.

Simply put, it decides, "Given a key identifying a request, and a rate, should we let a request through?"

- Lastly, the **Backend**. This is where the counters, records, basically all rate limit info about all the request seen by throttles in the application actually live. The main backends are `InMemoryBackend`, `RedisBackend`, and `MemcachedBackend`. The strategy reads from, and writes to it, as said earlier.

> Traffik provides an experimental in-memory backend - `MultiProcessInMemoryBackend`. This is different from the `InMemoryBackend` in that it allows data stored to be shared across multiple processes on the same machine correctly. The regualr in-memory backend is single process only. This is useful when you have to synchonize rate limiting on a one-machine setup with your application running on multiple workers without spinning up a distributed backend like Redis.

One more thing you would notice is that backend's have a `lifespan` context manager which we pass to the FastAPI instance on initialization as done in the example above. But to be fair, its not strictly needed as you can manage the backend lifespan manually if your setup requires it. It sets up and initializes the backend on application startup, and tears it down properly (as configured) on application shutdown. Backends themselves can be used as context managers in a middleware, in the endpoint code or even API service layer. You can read more about it in the documention.

## Backends

### In-memory backend - single process only, no dependencies

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp:inmemory")
app = FastAPI(lifespan=backend.lifespan)
```

With this backend, state lives in the process. It is fine for local development and single-worker deployments, and the wrong choice the moment you run more than one worker - each worker would count limits independently
and your real limit pere request becomes `configured_limit × worker_count`.

### Multi-process shared memory backend - multiple workers, one machine

> EXPERIMENTAL! This is a non-conventional one and still requires a lot of testing to prove its viability. It may be removed in future releases. This only works on UNIX platforms with `"fork"` process start method.

If you're running gunicorn/uvicorn with several workers on a single box and don't want
to stand up Redis just to get accurate counts across them then you can try out this backend.

I must say, setup may be trickier than other backends as it relies on process forking. Also context usage is constrained. You cannot use a closing context (`close_on_exit=True`) within the application with this one. Although, it is permitted at lifespan level

```python
from traffik.backends.multiprocess import MultiProcessInMemoryBackend

# Create before forking in your app factory or gunicorn's `on_starting`.
backend = MultiProcessInMemoryBackend(
    namespace="myapp",
    max_keys=65536,
    number_of_shards=64,     # rule of thumb: 2 × worker count
    cleanup_frequency=30.0,  # reclaim expired slots periodically
)
app = FastAPI(lifespan=backend.lifespan)
```

Workers attach to the same POSIX shared-memory segment after `fork`. Again it requires Linux, or macOS with
`multiprocessing.set_start_method("fork")` set explicitly (`fork` is already the default on Linux).

### Redis - with `redis.asyncio` client

This redis backend needs `redis.asyncio` as a dependency so you need to install it

```bash
uv add "traffik[aioredis]"
```

```python
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="myapp")
```

### Redis - with `coredis` client

This redis backend needs `coredis` as a dependency so you need to install it

```bash
uv add "traffik[coredis]"
```

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
support. It's the more mature client and measurably faster in our benchmarks (see
`benchmarks/`). Reach for `coredis` only when you need what it offers that `aioredis`
doesn't or you already use it as a client in your application and you dont want to install another client just for rate limiting.

### Memcached - with `aiomcache` client

This memcached backend needs `aiomcache` as a dependency so you need to install it

```bash
uv add "traffik[aiomcache]"
```

```python
from traffik.backends.memcached.aiomcache import MemcachedBackend

backend = MemcachedBackend(host="localhost", port=11211, namespace="myapp")
```

### Memcached - with `emcache` client (Linux/macOS, multi-node)

This memcached backend needs `emcache` as a dependency so you need to install it

```bash
uv add "traffik[emcache]"
```

```python
from traffik.backends.memcached.emcache import MemcachedBackend

backend = MemcachedBackend(
    nodes=["memcached://node1:11211", "memcached://node2:11211"],
    namespace="myapp",
)
```

This ones has Rendezvous hashing across nodes, adaptive connection pool, and has better overall throughput than `aiomcache`, although it is Linux/macOS only.

In summary, the required dependencies are;

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

Shown below are the main strategies Traffik provides for rate limting

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

# Example usage
throttle = HTTPThrottle("api", rate="100/min", strategy=SlidingWindowCounter())
```

When initializing a throttle, you can skip the throttle's `strategy` argument and it uses `FixedWindow` by default. It is the cheapest one, and works correctl for most APIs. It is also the only strategy that never needs a lock, so it is
fast on every backend, including the ones where locking gets expensive (see [Performance](#performance)).

Use `SlidingWindowCounter` if you encounter boundary bursts in practice, `TokenBucket` if you want to allow short bursts on top of a sustained rate, `GCRA` if you need genuinely even request spacing (think telecom-style
SLAs, not "please don't hammer my API").

### Advanced strategies

Other bespoke stragies available are in `traffik.strategies.custom`.

`traffik.strategies.custom` has six more: `TieredRateStrategy`,
`AdaptiveThrottleStrategy`, `PriorityQueueStrategy`, `QuotaWithRolloverStrategy`,
`TimeOfDayStrategy`, `CostBasedTokenBucketStrategy`.

Honestly, these exist because the problems were interesting to solve, not because most APIs need them. Per-tier limits, load-adaptive throttling, are real problems, bit just not *your* problem most of the time. Each has its own docs and example in the [full documentation](https://ti-oluwa.github.io/traffik/) if one of these genuinely
matches something you're dealing with.

## Rate formats

Traffik permits you to specify your rate limits in various string format, the most common of which you can see below.

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

Rate limit application and definition may differ by context and based on the way your code or endpoints are setup. You can use throttles as dependencies, middleware or even hit/test them directly, anywhere in the route or endpoint.

### FastAPI dependency

You can use throttles as dependencies on your routes:

```python
router = APIRouter(dependencies=[Depends(throttle)])
```

Or directly on your endpoint definitions:

```python
@app.get("/search", dependencies=[Depends(throttle)])
async def search():
    ...
```

### Starlette and FastAPI decorator

Throttles can also be used to decorate endpoints, if you prefer that;

```python
from traffik.throttle import HTTPThrottle

burst = HTTPThrottle("api:burst", rate="20/s")
sustained = HTTPThrottle("api:sustained", rate="500/min")
```

For Starlette;

```python
from traffik.throttles import throttled

@throttled(burst, sustained)
async def upload(request: Request):
    ...

route = Route("/upload", upload, methods=["POST"])
```

For FastAPI:

```python
from traffik.decorators import throttled

@app.post("/upload")
@throttled(burst, sustained)
async def upload(request: Request):
    ...
```

> Note that the decorators are imported from different path. The FastAPI specific decorator uses dependnecy injection under the hood, while the ones used for Starlette is a regular wrapper decorator and can be use for both Starlette and FastAPI.

### Middleware (blanket rules across routes)

Traffik provides the `ThrottleMiddleware` and `MiddlewareThrottle` classes for rate limiting in middleware.

> Your regular `HTTPThrottle` and `WebSocketThrottle` still work with the middleware directly and you dont always need to wrap them in `MiddlewareThrottle`.

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

>`path` and `methods` go on `MiddlewareThrottle`, not on `ThrottleMiddleware` itself. `ThrottleMiddleware` just runs whichever `MiddlewareThrottle`s match a given request.

### WebSockets

Yes, Traffik supports rate limiting websockets both at connection level and at per message level

```python
from traffik.throttles import WebSocketThrottle, is_throttled

ws_throttle = WebSocketThrottle("ws:messages", rate="30/min")

@app.websocket("/ws", dependencies=[Depends(ws_throttle)]) # Connection level
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await ws_throttle.hit(websocket, context={"scope": "message"}) # Per message level
        if is_throttled(websocket):
            # The default throttled handler already sent a *throttled* frame to the client
            # You can override that behaviour if you need something custom.
            continue
        await websocket.send_text(process(data))
```

## Custom identifiers

The default identifier used by throttles is the client IP. However, you can key/identify on whatever you want - user ID, API key, tenant ID, etc.

Example: Identifying by API key

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

## Cost-based throttling

Not all requests should count the same. Yes. You can specify cost per request or provide a function to compute it at runtime based on request info.

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

If clients need to be informed on how they are being rate limited so they can adjust their traffic accordingly, headers allows your API to include that information (conventionally). Although you will need to do this manually in your routes or throttled handler(s) if you need (custom) headers.

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

## Rules

Traffik allows you to gate when a throttle applies, or skip it for certain traffic using rules. See examples on how to use them below.

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

There are some cases where you only want a request to consume limit/quota if an operation actually succeeds, or may be you want to consume several throttles as one unit operation, a `QuotaContext` allows you to do this. See documentation for full API description or refer to module, class and method docstrings.

```python
from traffik.quotas import QuotaContext

async def create_order(request: Request):
    async with order_throttle.quota(request, lock=True) as quota:
        quota(cost=1)                       # this throttle
        quota(heavy_ops_throttle, cost=5)   # a different throttle, same transaction

        result = await do_expensive_work()
    # quota only consumed here, on clean exit
    return result
```

Consecutive calls to the same throttle with the same config aggregate into one backend
operation. `quota(cost=2); quota(cost=3)` becomes a single `increment(key, 5)`.

You can also check available quota without or before consuming it:

```python
async with throttle.quota(request) as quota:
    if not await quota.check(cost=10):  # Works similarly to `throttle.check(...)`
        raise HTTPException(429, "Insufficient quota")
    quota(cost=10)
    result = await heavy_work()
```

## Error handling and resilience

Errors occur always at runtime when processing requests. The throttling path is also prone to errors sometimes. Redis backend may hiccup due to temporary Redis unavailability. That raises the question, "What happens when the backend itself fails?". You can provide an error handler to handle these type of errors. The default handlers allow the throttle to fail open or close intelligently or switch to a fallback/backup backend temporarily until the main backned recovers. You could even switch backends permanently. Custom error handlers are also supported if you need one.

Example:

```python
throttle = HTTPThrottle("api", rate="100/min", on_error="allow")     # fail open
throttle = HTTPThrottle("api", rate="100/min", on_error="throttle")  # fail closed (default)
throttle = HTTPThrottle("api", rate="100/min", on_error="raise")     # handle it yourself
```

Fall back to a secondary backend with an automatic circuit breaker:

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

After 5 consecutive Redis failures, the circuit opens and new requests fall back to
in-memory immediately, no waiting on a timeout. It half-opens after 30 seconds, lets one
probe request through, and closes again on success.

For transient errors you can use th `retry` handler:

```python
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    on_error=retry(max_retries=3, retry_delay=0.05, retry_on=(TimeoutError,)),
)
```

## Dynamic backends (multi-tenant)

With Traffik, you can route different requests, clients, or even tenants to different backends at runtime, without having to define a unique throttles per case. Here's a simple example below. You can refer to the main documentation, implementaion docstrings or even look at the library's tests for more info on usage.

```python
throttle = HTTPThrottle("api", rate="100/min", dynamic_backend=True)

@app.get("/data")
async def data(request: Request):
    tenant_backend = get_backend_for_tenant(request)
    async with tenant_backend(request.app):
        return await throttle.hit(request) # Uses tenant specific backend in this block
    # Goes back to global backend on context exit
```

This adds roughly 1–20ms of backend-resolution overhead per request though - just something to note. Use an explicit
`backend=` instead, or leave it unset, if you don't actually need per-case backend routing and context switching.

## Runtime updates

Throttles have methods that allow you to change their configuration safely at runtime without recreating them. Some (if not all) of these methods include.

```python
await throttle.update_rate("200/min")
await throttle.update_strategy(TokenBucket())
await throttle.update_cost(5)
await throttle.disable()    # let everything through temporarily
await throttle.enable()
```

You can also disable globally, via the registry:

```python
from traffik.registry import GLOBAL_REGISTRY

await GLOBAL_REGISTRY.disable_all()   # Say in maintenance mode
await GLOBAL_REGISTRY.enable_all()
```

## Testing throttles - checking state without consuming it

You can check if the next request will exceeded the limit with `check` or `test` (alias) method.

```python
stat = await throttle.stat(request)
if stat:
    print(f"{stat.hits_remaining} hits left, {stat.wait_ms}ms until next request allowed")
    print(stat.metadata)  # strategy-specific details

# or just a boolean
if not await throttle.check(request, cost=5):
    raise HTTPException(429, "Not enough quota for this operation")
```

## Performance

Two things determine your actual overhead, and they are not necessarily coupled. They are; the backend
you pick, and the strategy you pick.

The **Backend** chosen decides your baseline cost. In-memory has no network or IPC involved. You get sub-
millisecond backend ops. Redis and Memcached are dominated by the round trip to wherever
those services live, not by anything Traffik itself is doing. Multi-process shared
memory skips the network entirely, but that's not a free lunch either.

Your **Strategy** decides whether you pay a locking tax on top of that baseline.
`FixedWindow` (`GCRA` too) does one atomic increment and never takes a lock and is therefore cheap on every backend,
including the ones where locking is expensive. `TokenBucket` and anything else that
needs to read state, do math, and write it back *has* to hold a lock across all three
steps, or two concurrent requests can both read "3 tokens left" and both spend one and then record "two tokens left",
which quietly breaks your rate limit. It is not a bug, it is the correctness costs we have to pay for
those types of algorithms. However, that means the backend you would otherwise pick on gut feel can
behave differently once a distributed lock is involved.

**Multi process backend cavaet - Why it may not be better than just using Redis or Memcached.**

The multi-process backend can get more expensive than you would normally expect.
Every read and write has to hop through a thread pool (`run_in_executor`) because
the underlying primitives are blocking, not async. `FixedWindow` pays that tax once per
request. `TokenBucket` pays it twice - once for the read, once for the write, while
holding a per-shard lock the whole time. Stack fifty concurrent requests on one hot key
and that adds up fast. Hence, you need to avoid hot keys with this backend.

Redis (local), despite paying real network round trips for the same **get-lock-read-write-unlock** sequence, ends up faster here, because a socket write to localhost is cheaper than a thread-pool handoff plus an OS semaphore under contention. Hosted Redis may perform simlar to or slightly better than the multi-process backend for this scenario.
Not the result I'd have guessed either, and I believe it's a good reminder that "avoids the network" doesn't automatically mean "faster."

None of this is something you should take on my word without checking. You can run benchmarks yourself, against your own traffic shape (key cardinality, concurrency, strategy).

### Running the benchmarks

Traffik includes a simple bechmark suite for performance and correctness testing. It tests throughput, latency and correctness per case run. Benchmarks are not the *"one-all-be-all"* but they can be a good starting point to get insight on how throttling may perform with specific traffic patterns. Moreover, I can't guarantee that the benchmarks I write are perfect or unbiased so take the results with a pinch of salt.

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
running locally. `docker compose up -d redis memcached` handles that if you don't
already have them.

Results print as a table by default; pass `--output json` if you want to feed them into
something else. `--concurrency` controls how wide the concurrent-scenario batches are
(default 50), `-n`/`--iterations` controls how many timed runs you get per scenario
(default 3, plus one discarded warmup run).

## Full documentation

[https://ti-oluwa.github.io/traffik/](https://ti-oluwa.github.io/traffik/) - Advanced
strategies, WebSocket per-message throttling, testing patterns, full API reference.
Everything that didn't fit here.

## Contributing

Issues and PRs welcome. `make dev-setup` gets you a working dev environment,
`make test-fast` for a quick sanity check before you push, `make quality` before you
open a PR. See `CONTRIBUTING.md` for the actual details.

## License

MIT
