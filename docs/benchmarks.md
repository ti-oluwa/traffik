# Benchmarks

The benchmark suite exercises Traffik the way it actually runs in production: as a real server process, listening on a real socket, handling real HTTP and WebSocket connections, no just as a Python function called directly in the same process. This page documents how the suite works, how to run it, and how to read its output.

!!! note "No result numbers here"
    This page deliberately doesn't publish throughput or latency figures. Results depend heavily on your machine (CPU core count especially. See [What To Expect](#what-to-expect) below), so numbers from one machine are not a reliable stand-in for another's. Run the suite yourself to get figures that reflect your setup.

---

## How It Works

Every benchmark run:

1. Spawns the target app as its own OS process. `uvicorn` for a single worker, or `gunicorn` (with `--preload` and the `fork` start method) for multiple workers.
2. Waits for the process to answer a health check before sending any traffic.
3. Drives real traffic against it: `httpx2.AsyncClient` over a TCP connection for HTTP/middleware scenarios, and the `websockets` library for WebSocket connections.
4. Resets the throttle backend's state via an admin endpoint between every iteration, so later iterations aren't skewed by counters left over from earlier ones.
5. Tears the process down before moving to the next scenario.

One process is spun up per scenario and reused across that scenario's warmup and timed iterations, then shut down. It isn't restarted for every single request, and it isn't shared across different scenarios.

Because the app runs as a real process, gunicorn's forked workers behave exactly as they would in a real deployment: `MultiProcessInMemoryBackend.start()` runs once in the master process before `fork()`, and every worker shares that state for real, rather than the benchmark merely asserting that it should.

---

## Installation

The benchmark suite has its own dependency group so it doesn't bloat a normal install:

```bash
uv sync --group benchmark --inexact
# or
pip install "traffik[benchmark]"
```

This pulls in `click`, `rich`, `fastapi`, `uvicorn`, `gunicorn` (POSIX only), `websockets`, `psutil` (memory sampling, for `scale`), `slowapi` (for `compare` and `sweep`), `matplotlib` (for `--plot`), and the backend client libraries. `gunicorn` isn't available on Windows as anything requiring more than one worker process needs a POSIX system (Linux or macOS).

If you want to benchmark against Redis or Memcached instead of the default in-memory backend, start real instances first:

```bash
docker compose up -d redis memcached
```

Any backend other than `inmemory` or `multiprocess` needs a real, reachable server. The suite does not stub these out.

---

## Running Benchmarks

The suite is a `click`-based CLI with seven commands: four benchmark one integration pattern each, and three answer a different kind of question: `compare` (how traffik stacks up against SlowAPI at one load), `sweep` (where each of them stops scaling as load rises), and `scale` (how traffik behaves as key cardinality grows).

```bash
python -m benchmarks http
python -m benchmarks middleware
python -m benchmarks websocket
python -m benchmarks multiprocess
python -m benchmarks compare
python -m benchmarks sweep
python -m benchmarks scale
```

Or via the Makefile shortcut, which forwards any arguments after `bench`:

```bash
make bench "http --scenarios under_limit,over_limit"
```

Each command accepts `--help` for the full option reference.

### Common Options

These options are shared across `http`, `middleware`, `websocket`, and `multiprocess`. `compare`, `sweep` and `scale` overlap heavily but not exactly - see their own sections below (`compare` adds `--mode` and `--endpoint`; `sweep` replaces `--concurrency` and `--scenarios` with `--levels`, `--distribution` and friends; `scale` drops `--iterations`/`--warmup`/`--scenarios` and adds `--checkpoints`/`--mp-max-keys`).

| Option | Short | Default | Description |
| --- | --- | --- | --- |
| `--backend` | `-b` | `inmemory` | Backend to benchmark. See [Backends](#backends) below. |
| `--strategy` | `-s` | `fixed_window` | Throttling strategy to benchmark. See [Strategies](#strategies) below. |
| `--iterations` | `-n` | `3` | Number of timed iterations per scenario. |
| `--warmup` | `-w` | `1` | Number of warmup iterations run (and discarded) before timing starts. |
| `--concurrency` | `-c` | `50` | Requests kept in flight in scenarios that send concurrent traffic. |
| `--workers` | `-W` | `1` (`4` for `multiprocess`) | Number of real worker processes serving the app. `1` spawns a single `uvicorn` process. Greater than `1` spawns `gunicorn` with `--preload`, forking that many real worker processes (POSIX only). |
| `--output` | `-o` | `table` | `table` (rich-rendered) or `json`. |
| `--redis-url` | | `redis://localhost:6379/0` | Connection URL, used when `--backend` is `aioredis` or `coredis`. |
| `--memcached-host` | | `localhost` | Used when `--backend` is `aiomcache` or `emcache`. |
| `--memcached-port` | | `11211` | Used when `--backend` is `aiomcache` or `emcache`. |
| `--scenarios` | | `all` | Comma-separated scenario names, or `all`. |
| `--plot` | | off | Write charts for the run into this directory (created if missing). Needs `matplotlib`. See [Graphs](#graphs). Also available on `compare`, `sweep` and `scale`. |
| `--plot-format` | | `svg` | `svg` or `png`, for `--plot`. |
| `--mp-max-keys` | | `65536` | `multiprocess` only: capacity of the fixed-size shared-memory table. See `key_expiry_reuse`. |
| `--no-gate` | | off | `http`, `middleware`, `compare`, `sweep`: disable the process-local lock contention gate, to measure what it buys. A diagnostic, not a recommended setting. Networked backends only; ignored otherwise. |
| `--lock-contention-threshold` | | backend default | Same flags' finer-grained form: waiters on one lock name before the gate starts serializing them. |

!!! warning "Workers and the in-memory backend"
    `--workers` greater than `1` combined with `--backend inmemory` will print a warning and still run, but the result is not meaningful: each forked worker gets its own independent copy of in-memory state, so requests routed to different workers won't see each other's counters. Use `--backend multiprocess` (or an external backend like `aioredis`/`coredis`) if you want to see real throttling behaviour across multiple worker processes.

---

## Commands and Scenarios

### `http` - Dependency-Based Throttling

Benchmarks the most common integration pattern: a throttle injected via `Depends(throttle)` on a single endpoint.

Scenario names say what the traffic does. The concurrent ones run at `--concurrency` requests in flight, and differ only in key distribution and whether the limit is hit, so they can be compared directly (see [What each scenario measures](#what-each-scenario-measures)).

| Scenario | Traffic | Limit | What it is for |
| --- | --- | --- | --- |
| `under_limit` | 80 sequential requests | 200 | Per-request overhead with no contention and no rejection. |
| `at_limit` | 101 sequential requests | 100 | The same, plus a boundary check: exactly 100 allowed, then exactly 1 rejected. |
| `over_limit` | 200 sequential requests | 50 | The cost of the rejection path. |
| `hot_key_under_limit` | 800 concurrent requests, all one `X-Client-ID` | 1000 | Pure single-key serialization: nothing is rejected, so req/s is how fast one key's critical section drains. |
| `hot_key_over_limit` | 300 concurrent requests, all one `X-Client-ID` | 100 | Single-key contention plus rejection, the shape of one abusive client. |
| `many_keys_under_limit` | 800 concurrent requests, as many distinct identities as requests in flight | 1000 | The no-contention baseline: same requests, concurrency and limit as `hot_key_under_limit`, so the gap between the two is the cost of sharing a key. |
| `window_rollover` | Three waves of 20 sequential requests, 1.1 s apart | 20 per second | Does each new window grant a fresh allowance? |

!!! note "Renamed scenarios"
    Earlier versions called these `below_limit`, `hot_key`, `many_keys`, `sustained` and `window_boundary`, and had `concurrent` and `error_recovery`. `concurrent` sent no `X-Client-ID`, so every request shared the client IP's key: it was a second hot-key scenario, now covered by the two `hot_key_*` ones. `error_recovery` injected no errors (it was `at_limit` with `on_error="allow"` on a healthy backend), so it measured nothing the others don't. A real failure-path benchmark needs a fault-injecting backend.

### `middleware` - Middleware-Based Throttling

Benchmarks `ThrottleMiddleware` with a `MiddlewareThrottle` entry, applied without touching route handlers. Includes the same seven scenarios as `http` (display names prefixed with `Middleware`), plus:

| Scenario | Traffic | What it is for |
| --- | --- | --- |
| `exempt_path` | 100 requests to a throttled path, then 100 to an exempt one, limit 50 | Exempt routes must pay no throttle cost and the throttled route must still be enforced: exactly 25% of all requests rejected. |

### `websocket` - WebSocket Throttling

Benchmarks a single throttled `/ws` endpoint over real WebSocket connections.

| Scenario | Traffic | What it is for |
| --- | --- | --- |
| `under_limit` | 50 messages on one connection, limit 100 | Per-message round trip with nothing rejected. |
| `over_limit` | 150 messages on one connection, limit 50 | The cost of the rejection path over a WebSocket. |
| `shared_key_connections` | 10 concurrent connections of 20 messages, limit 1000 | Connection fan-in onto one key (every connection shares the client IP's key), nothing rejected. |
| `window_rollover` | Three waves of 10 messages, 1.1 s apart | 10 per second. Does each new window grant a fresh allowance? |

### `multiprocess` - Real Multi-Worker State Sharing

Benchmarks `MultiProcessInMemoryBackend` across real, forked `gunicorn` workers (POSIX only). This command forces `--backend multiprocess` regardless of what `--backend` is passed, and reuses the same `Depends`-based endpoint as `http`. It includes the same seven scenarios as `http` (prefixed `MP`), plus two that specifically stress the shared-memory backend:

| Scenario | Traffic | What it is for |
| --- | --- | --- |
| `many_keys_across_shards` | 2000 concurrent requests over 1000 distinct identities, limit 100 per key | Shard parallelism: many keys should spread across shards and workers instead of queuing. Compare with `hot_key_under_limit`. |
| `key_expiry_reuse` | 500 sequential requests over 500 distinct keys against 100 per 2 s, in two halves with a 6 s pause | Expired slots must be reclaimed. Run it with `--mp-max-keys 375` (about 1.5x one half's 250 keys): one half fits with room for uneven hashing, both together do not, so the second half only succeeds if expired slots are reclaimed. At the default capacity nothing is ever full and it passes trivially; below one half (say 120) it fails, which is how you know it is sensitive. |

!!! warning "`--workers` below 2"
    Running `multiprocess` with `--workers` set below `2` prints a warning: gunicorn won't actually fork multiple workers, so the run won't exercise any cross-process state sharing. Set `--workers` to at least `2` (and realistically, to your CPU core count) to test what this command is for.

### `compare` - traffik vs SlowAPI, Under Matched Conditions

Runs each selected scenario against both a traffik app and a [SlowAPI](https://github.com/laurentS/slowapi) app, one after the other, and reports both side by side.

What's held identical between the two apps for a given run:

| Requirement | How `compare` handles it |
| --- | --- |
| Identical algorithm | `--strategy` accepts `fixed_window` or `sliding_window_counter` - the only two algorithms traffik and `limits` (SlowAPI's engine) implement the same way. `sliding_window_counter` exercises more of traffik's locking (it reads the current *and* previous window, combines them, then increments, all under `backend.lock(...)`) than `fixed_window`'s simpler path, so it's the better one for seeing lock overhead specifically. |
| Identical backend | Same `--backend` value maps to the matching storage on both sides (`inmemory` → `memory://`, `aioredis`/`coredis` → the same `--redis-url`, `aiomcache`/`emcache` → the same `--memcached-host`/`--memcached-port`). `--backend multiprocess` is not offered: SlowAPI/`limits`' `memory://` storage is a plain in-process dict with no fork-safety story, so there is no fair, identical-backend comparison to run against `MultiProcessInMemoryBackend`. Use `aioredis` if you want a multi-worker-safe comparison instead. |
| Identical worker count | `--workers` is passed to both apps unchanged. |
| Both integration patterns | `--mode http` (default) compares per-route `Depends(throttle)` against `@limiter.limit(...)`; `--mode middleware` compares `ThrottleMiddleware` against a small ASGI middleware calling `limits` directly (SlowAPI has no global-middleware mode of its own). Both modes apply the throttle to `/test` only, leaving `/unthrottled` exempt. |
| Identical key cardinality, hot-key and many-key traffic, under-limit and over-limit traffic | Reused directly from `HTTP_SCENARIOS`/`MIDDLEWARE_SCENARIOS` (`--scenarios`) - both sides run the literal same scenario definition, not separately-tuned equivalents. |
| Identical rate | SlowAPI's rate string is derived from the same `Rate` object traffik parses (`benchmarks/rates.py`), not a hand-maintained second copy that could quietly drift from traffik's. |
| p50/p95/p99 latency | Reported for both sides, plus a computed req/s delta. |
| Sync and async endpoint variants | `--endpoint async` (default) hits `/test` (`async def`) on both apps; `--endpoint sync` hits `/test-sync` (plain `def`) on both. Only applies to `--mode http`; middleware mode has no sync variant to compare. |
| Redis local vs. remote latency | Not special-cased in code - just re-run with `--redis-url` pointed at a local vs. a remote Redis to see the difference; both apps read the same `--redis-url`. |

```bash
python -m benchmarks compare --backend inmemory --scenarios under_limit,hot_key_under_limit,many_keys_under_limit
python -m benchmarks compare --backend inmemory --strategy sliding_window_counter
python -m benchmarks compare --backend inmemory --mode middleware
python -m benchmarks compare --backend aioredis --redis-url redis://localhost:6379/0 --endpoint sync
python -m benchmarks compare --backend inmemory --plot ./bench-plots   # adds the throughput-vs-tail trade-off chart
```

!!! warning "What this does *not* control for"
    The two apps run **sequentially**, not side by side - each gets the machine to itself while it's being measured, specifically so neither app's traffic competes with the other's for CPU or (for external backends) the same Redis/Memcached connections during its own measurement window. That said, this is still one run, on one machine, with everything else about your system uncontrolled (other processes, thermal throttling, background load). Treat a single `compare` run as a data point, not a verdict - run it more than once, and read the actual numbers rather than just the sign of the delta.

### `sweep` - Where Does Each One Stop Scaling?

A single fixed-concurrency run, which is all `compare` gives you, answers "how fast at this load?". It cannot say where an implementation stops scaling, or whether a lower peak buys a better tail. `sweep` runs one workload at increasing numbers of requests in flight (`--levels`, default `10,50,100,200,400`) and records throughput and P50 to P99.9 at each, for:

- a **hot key** (every request shares one identity), which maximizes lock contention, and
- **many keys** (no two in-flight requests share a key), which shows whether contention is spread out.

Each is run against traffik and, when comparable, SlowAPI, so a plot of either distribution shows both curves. The rate limit defaults to `1000000/60s`, far above the load, so nothing is rejected: what is left is the cost of the throttle's own synchronization.

```bash
python -m benchmarks sweep --backend aiomcache --plot ./bench-plots
python -m benchmarks sweep --backend aiomcache --strategy fixed_window --plot ./bench-plots   # control: no read-compute-write critical section
python -m benchmarks sweep --backend aiomcache --diagnose-gate --plot ./bench-plots           # adds a "traffik (no gate)" series
```

| Option | Default | Description |
| --- | --- | --- |
| `--levels` | `10,50,100,200,400` | Requests in flight at each point. |
| `--distribution` | `both` | `hot`, `many` or `both`. |
| `--requests` | `3000` | Requests per point per iteration. P99.9 needs 1,000+ latency samples across iterations to mean anything; it is shown as `-` below that. |
| `--keys` | `1000` | Distinct keys for the many-key distribution (raised to the level when lower, so no two in-flight requests share a key). |
| `--rate` | `1000000/60s` | Keep it far above `--requests`. |
| `--no-slowapi` | off | Only run traffik. SlowAPI is skipped automatically for strategies and backends it has no equivalent for. |
| `--diagnose-gate` | off | Also run traffik with the process-local lock contention gate disabled, as a third series. Separates the cost of the distributed lock from the cost of the local gate in front of it. Networked backends only. |

The two control runs are what make the result interpretable. Running `--strategy fixed_window` (a single atomic increment) next to `sliding_window_counter` (a locked read, compute and write) shows how much of any gap with SlowAPI comes from the locking. `--diagnose-gate` shows what the gate trades: if disabling it raises throughput but worsens P99 and P99.9, the gate is buying tail behavior with peak throughput.

!!! warning "What a sweep does not tell you"
    The load is a **closed loop**: a fixed number of requests are kept in flight and each worker sends its next request as soon as its previous response arrives. That finds where each implementation stops scaling, but it understates the tail an arrival rate the system cannot keep up with would produce, because the queue forms in the client instead. The load generator also shares the machine with the server, so if req/s falls as in-flight rises even for many keys, the client is part of the bottleneck. Read the *shape* of each curve (where throughput flattens and P99 starts climbing), not a single point, and treat one sweep as a data point.

### `scale` - Memory Pressure and Scalability

Starts **one** traffik app server and keeps it running for the entire command - unlike every other command here, which restarts the server before each iteration. Growing the backend's distinct-key count while it keeps running is the point: a fresh process per measurement would hide exactly the memory accumulation and latency drift this command exists to show.

For each `--checkpoints` value (cumulative, ascending distinct-key counts), it sends that many new requests - each carrying a brand-new identity - concurrently (`--concurrency` in flight at once), then samples:

- The server's memory via `psutil`: the process plus every worker it forked, using proportional set size where the platform has it so pages shared between workers (the multiprocess backend's segment) are counted once. With `--workers` above 1 the process we start is only gunicorn's master, so measuring it alone would miss the workers that hold the keys. The server is sent a few hundred throwaway requests before the baseline is taken, so one-time allocations (route caches, connections) don't land in the first checkpoint and inflate its per-key cost.
- For `aioredis`/`coredis`, also the Redis server's own `INFO memory` `used_memory` (best-effort; the app process's RSS mostly reflects connection overhead for these backends, not the key data itself).
- That batch's mean req/s, P50, and P99 latency - watch these for drift as the backend fills up, not just the memory columns. Real concurrent access at scale, not just raw key count, is half of what this command is for.

```bash
# Get a feel for it first - the default checkpoints go up to 1,000,000 and can take a while.
python -m benchmarks scale --backend inmemory --checkpoints 1000,10000

# Then something closer to production scale:
python -m benchmarks scale --backend inmemory --checkpoints 10000,100000,1000000 --concurrency 200
python -m benchmarks scale --backend multiprocess --checkpoints 10000,100000 --shards 64
```

!!! note "Reading the multiprocess numbers"
    The multiprocess backend's table is allocated up front and only counts toward memory as its pages are touched, so the cumulative bytes per key is dominated by that fixed cost at small checkpoints. Compare the memory growth *between* checkpoints rather than the first one. When `--mp-max-keys` isn't given, the table is sized to twice the largest checkpoint plus the warmup keys: keys hash unevenly across shards and each shard's capacity is fixed, so a table sized to exactly the key count would overflow some shards early and answer `ShardFullError` 500s at the last checkpoint.

`--mp-max-keys` (for `--backend multiprocess`) defaults to twice the highest `--checkpoints` value plus the warmup keys. That backend's shared-memory table is fixed-size and sized once at startup, so it can't grow past what it was told to expect, and the headroom is what keeps uneven hashing from overflowing a shard. The first checkpoint's memory jump reflects the table being touched for the first time, not a per-key cost, which is a real property of that backend's memory profile rather than a benchmark artifact.

The `Bytes/key` column is `cumulative Δ RSS ÷ cumulative keys` - a rough running average, not a precise per-key allocation figure (it also carries whatever fixed overhead the backend paid once at startup, amortized over however many keys exist by that checkpoint).

---

## Backends

Set with `--backend` / `-b`:

| Value | Description |
| --- | --- |
| `inmemory` | Single-process in-memory state. No external services needed. Not safe to share across multiple worker processes - see the warning above. |
| `multiprocess` | Shared-memory state, safe across real forked worker processes. See [`MultiProcessInMemoryBackend`](core-concepts/backends.md) for how it works. |
| `aioredis` | Redis via `redis.asyncio`. Requires a reachable Redis server (`--redis-url`). |
| `coredis` | Redis via `coredis`. Requires a reachable Redis server (`--redis-url`). |
| `aiomcache` | Memcached via `aiomcache`. Requires a reachable Memcached server (`--memcached-host` / `--memcached-port`). |
| `emcache` | Memcached via `emcache`. Not available on Windows. Requires a reachable Memcached server. |

## Strategies

Set with `--strategy` / `-s`:

`fixed_window`, `sliding_window_counter`, `sliding_window_log`, `token_bucket`, `token_bucket_debt`, `leaky_bucket`, `leaky_bucket_queue`, `gcra`.

See [Strategies](core-concepts/strategies.md) for what each one does and when to reach for it.

---

## Fairness and Known Limits

A comparison is only worth reading if the harness does not favor a side. What the suite does to keep it even, and what it cannot fix:

**Controlled on both sides**

- Same server command (uvicorn, or gunicorn with the same workers), same machine, same rate, strategy, identity rule, scenario traffic and number of iterations. Each side runs against a fresh server, and state is reset between every iteration.
- The two sides run one after the other, never together, and **which one goes first alternates** from scenario to scenario (and from point to point in `sweep`), so neither owns the cooler-CPU first slot.
- The load is a true closed loop: exactly `--concurrency` requests are kept in flight, and each worker sends its next request as soon as its previous response arrives. Sending fixed batches and waiting for the slowest request of each (as earlier versions did) ties throughput to the worst request in every batch, which rewards whichever implementation has the better tail and understated the other's throughput by up to a third.
- Latency covers **every request sent, including failed and timed-out ones** (they appear in Error %). Dropping them would make an implementation that times out look faster.
- req/s counts **answered requests** (allowed plus throttled), so failing fast never looks like speed. Percentiles use the nearest-rank method; the old `int(n * p)` indexing made P99 the maximum for any run of 100 samples or fewer.
- Memcached reset flushes the server on both sides, so traffik does not pay the extra round trips of `track_keys` for a reset convenience SlowAPI doesn't use.
- The compare table puts no red or green on req/s except for `T` and `L` scenarios, ignores gaps inside the run-to-run spread (marked `≈`), and shows the P99 change next to the req/s change.

**Not controllable, so read results with them in mind**

- **SlowAPI does blocking I/O.** `limits` uses synchronous Redis and Memcached clients, so each call blocks the event loop. On loopback, where a round trip is microseconds, that costs it almost nothing; against a real network, with a millisecond or more per round trip, blocking serializes its requests while traffik overlaps its I/O. A benchmark on one machine therefore flatters a blocking client, and it is the main reason to treat a local result as a lower bound for the asynchronous design.
- **The algorithms are not identical.** Both do the same strategy on paper, but traffik's `sliding_window_counter` holds a lock around read, compute and write (more round trips), while `limits` updates optimistically. `fixed_window` is closest to a like-for-like comparison, which is why `--strategy fixed_window` is the control run.
- **Fixed windows are anchored differently.** traffik's windows are aligned to the clock; `limits` starts a window at the first request. Allowed counts agree, but a run that straddles a window boundary can differ between the two.
- **Connection pools differ by design.** Each library runs with its defaults (for example, `aiomcache`'s pool of 2 connections in traffik versus one blocking connection in `limits`).
- **One machine.** The load generator shares CPU with the server and the backend. If req/s falls as concurrency rises even on many keys, the client is part of the bottleneck.
- **Small samples.** Three iterations is enough to see large effects and not small ones. Raise `--iterations` before claiming a gap of a few percent.

## Reading the Output

### Table Output (default)

A `rich`-rendered table, one row per scenario, aggregated across all timed iterations (warmup iterations are discarded and never shown). Under it is a **glossary** with one row per scenario that ran: what it tests, how to read it, its caveat, and what a good result looks like. Columns:

| Column | Meaning |
| --- | --- |
| Scenario | Scenario display name. |
| Type | What the scenario's req/s actually measures: `T`, `S`, `R`, `L`, `P` or `B`. See [What each scenario measures](#what-each-scenario-measures). |
| Backend / Strategy | What was benchmarked. |
| req/s | Mean answered requests (allowed plus throttled) per second across iterations, **excluding intentional pauses** (the sleeps between waves). Failed requests are not counted. |
| P50 / P95 / P99 | Latency percentiles (nearest-rank), in milliseconds, pooled across all timed iterations. Includes failed requests. |
| P99.9 | The 99.9th percentile. Shown only with 1,000+ latency samples; below that it is just the maximum, so it shows `-`. |
| Success % | Percentage of requests that received a `200`. |
| Throttled % | Percentage of requests that received a `429`. |
| Error % | Percentage of requests that failed for any other reason (connection errors, timeouts, unexpected status codes). |

### What each scenario measures

A rate limiter benchmark mixes scenarios that answer different questions, and one req/s column hides that. Reading "lower req/s" as "slower" is the usual mistake. Every scenario carries a type:

| Type | Name | req/s is... | Read mainly |
| --- | --- | --- | --- |
| `T` | Throughput | Close to capacity: independent keys, nothing rejected. | req/s |
| `S` | Serialization | How fast one key's critical section drains: every request shares a key. | P99 and P99.9, not req/s |
| `R` | Rejection path | Dominated by cheap 429s, so a faster rejection raises it without doing more useful work. | Throttled % and the allowed rate |
| `L` | Latency | Just 1 / latency: one request in flight. | P50 to P99 |
| `P` | Paced | Contains deliberate pauses (excluded from req/s), so it is still a latency figure. | Success % and Throttled % |
| `B` | Behavior | Not a speed test: it checks that a behavior holds. | Errors % and the glossary's "good looks like" |

**Throughput and latency are different questions.** Throughput is an aggregate (requests completed per second); latency is per request. At a fixed number of requests in flight they are tied together (Little's law: in flight = throughput x average latency), but between two implementations they are not: a design that lets many requests hit the backend at once can post a slightly higher peak while queues build and the tail gets worse, and a design that serializes a hot key deliberately can post a slightly lower peak with a flatter tail. So on an `S` scenario, "traffik is a few percent lower on req/s and lower on P99" is a trade, not a contradiction. On a `T` scenario, where nothing contends, it should not need one.

The concurrent scenarios form a grid, so a difference can be attributed:

| | One key (every request shares it) | Many keys (none shared in flight) |
| --- | --- | --- |
| Under the limit | `hot_key_under_limit` (`S`) | `many_keys_under_limit` (`T`) |
| Over the limit | `hot_key_over_limit` (`S`) | |

The first row has the same requests, concurrency and limit and nothing rejected in either, so the gap between its two cells is purely the cost of key contention. To find where a gap turns into a collapse, use `sweep`.

### Graphs

Pass `--plot DIR` to `http`, `middleware`, `websocket`, `multiprocess`, `compare`, `sweep` or `scale` to write charts for that run into `DIR` (SVG by default, `--plot-format png` for PNG). They are generated from the run you just did, so there is nothing to render separately, and the file names carry the mode, backend, strategy and worker count so runs do not overwrite each other. The paths are listed on stderr, so `--output json` stays clean on stdout.

| Command | Charts |
| --- | --- |
| `http`, `middleware`, `websocket`, `multiprocess` | `-throughput` (req/s per scenario, colored by type so a serialization scenario is never mistaken for capacity), `-latency` (P50/P95/P99, plus P99.9 when there are enough samples, log scale), `-outcomes` (allowed, throttled and error share of requests). |
| `compare` | `-throughput` and `-latency` (traffik next to SlowAPI), and `-tradeoff`: each scenario as a point of req/s change against P99 improvement. The upper-left quadrant is "less throughput, better tail", and where the `S` scenarios should land if serialization is doing its job. |
| `sweep` | `-throughput`, `-p99` and `-p999` against requests in flight (one panel per key distribution; look for where throughput flattens and the tail starts to climb), and `-hot-key-cost` (hot-key req/s divided by many-key req/s; 1.0 means sharing a key costs nothing). |
| `scale` | `-memory`, `-bytes-per-key` and `-latency` against distinct keys. |

### JSON Output

Pass `--output json` for machine-readable results - useful for feeding into your own reporting or tracking regressions over time in CI. The structure is:

```json
{
  "meta": {
    "backend": "...",
    "strategy": "...",
    "iterations": 3,
    "warmup_iterations": 1,
    "workers": 1,
    "timestamp": "...",
    "platform": "...",
    "python_version": "..."
  },
  "results": [
    {
      "scenario_name": "...",
      "backend_kind": "...",
      "strategy_kind": "...",
      "iterations": 3,
      "total_requests": 0,
      "mean_rps": 0.0,
      "p50_ms": 0.0,
      "p95_ms": 0.0,
      "p99_ms": 0.0,
      "p999_ms": 0.0,
      "sample_count": 0,
      "mean_ms": 0.0,
      "mean_allowed_rps": 0.0,
      "mean_throttled_rps": 0.0,
      "success_rate": 0.0,
      "throttle_rate": 0.0,
      "error_rate": 0.0,
      "rps_stddev": 0.0
    }
  ]
}
```

---

## What To Expect

A few things are worth understanding before you interpret a run, so you don't mistake expected behaviour for a bug.

**Success/throttle percentages should match the configured rate.** For a scenario sending `N` requests against a rate that permits `M` of them, expect roughly `M/N × 100` success and the rest throttled (the `*_under_limit` scenarios should allow everything). `at_limit` should show exactly 99.0% allowed and 1.0% throttled. If these don't line up (barring a window boundary, below), something's wrong with the run, not with your expectations.

**Numbers reflect real network and process overhead - by design.** Because this suite drives real HTTP/WebSocket traffic against a real server process, every request pays for a real TCP round trip, real HTTP/1.1 framing, and real ASGI request handling. That overhead did not exist in earlier versions of this suite (which called the ASGI app directly in-process) and won't disappear here - it's an accurate reflection of what a deployed instance actually costs per request, not a regression.

**Concurrency and `--workers` only pay off with real CPU cores.** `asyncio` concurrency helps most when there's real I/O wait time to overlap; on loopback that wait time is minimal, so a single worker process is largely CPU-bound on request parsing and routing. Multiple `gunicorn` workers only run in true parallel if there are separate physical cores for them to run on, i.e, on a single-core machine, `--workers 4` will look barely different from `--workers 1`, because there's only one core for either to use. If you want to see `--workers` make a real difference, set it based on how many cores your machine actually has and compare against a `--workers 1` run of the same scenario.

**Fixed and sliding windows are clock-aligned, so a run can straddle a window boundary.** A 60-second window ends on the wall-clock minute, wherever your run happens to be. If a scenario's requests straddle that boundary, the throttle legitimately grants a second allowance and Success % comes out higher than `limit / requests` (for example 48% instead of 25% on `over_limit`). Rerun it. `window_rollover` is the deliberate version of this: three waves 1.1 s apart against a 1 s window, where every wave should land in a fresh window under `fixed_window`.

**Warmup iterations are discarded on purpose.** The first iteration against a freshly-started process can be slower (import caches warming, initial connection setup); warmup iterations exist to absorb that before timed iterations begin. Increase `--warmup` if you still see a slow first timed iteration.

---

## Troubleshooting

**`ERROR: Could not start server for <scenario>`**: the spawned `uvicorn`/`gunicorn` process failed its health check within the startup timeout. The error includes a tail of the process's stderr; check it first. Common causes: a missing dependency for the selected backend, or a backend that requires a running external server (Redis/Memcached) that isn't reachable.

**`--workers` greater than `1` fails outright**: this requires a POSIX system. `gunicorn`'s worker model relies on the `fork` start method, which Windows does not support. The `multiprocess` command is unavailable on Windows entirely for the same reason.

**Connection refused for `aioredis`/`coredis`/`aiomcache`/`emcache`**: start the relevant service first (`docker compose up -d redis memcached`), or point `--redis-url` / `--memcached-host` / `--memcached-port` at a server that's actually running.

**A scenario reports a nonzero error rate**: this means requests failed for a reason other than throttling (connection errors, timeouts, unexpected responses). It shouldn't happen in a healthy run; check the scenario's stderr output and the backend you selected.

**`compare`/`sweep`/`scale` fail on import with a missing `slowapi`/`psutil`**: these are benchmark-only dependencies (see [Installation](#installation)) not needed by the other four commands; a plain `uv sync --group benchmark` or `pip install psutil slowapi` picks them up.

**`--plot` says it needs matplotlib**: it is part of the benchmark group (`pip install "traffik[benchmark]"`). The check runs before the benchmark starts, so a long run is never wasted on a missing dependency.

---

## Extending the Suite

Scenarios are declarative specs, not hand-written functions. See `benchmarks/scenarios.py`. Adding a new scenario to an existing command means adding an entry to the relevant registry (`HTTP_SCENARIOS`, `MIDDLEWARE_SCENARIOS`, `WEBSOCKET_SCENARIOS`, or `MULTIPROCESS_SCENARIOS`) with a rate, request count, and traffic pattern (`sequential`, `concurrent`, `waves`, `unique_keys_concurrent`, `unique_keys_split`, or `mixed_paths` for HTTP-like scenarios). The actual traffic-generation logic lives in `benchmarks/live/runners.py` and is shared across every scenario of that shape. You shouldn't need to touch it to add a new scenario, only to add a genuinely new traffic pattern.
