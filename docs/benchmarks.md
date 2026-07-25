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

This pulls in `click`, `rich`, `fastapi`, `uvicorn`, `gunicorn` (POSIX only), `websockets`, and the backend client libraries. `gunicorn` isn't available on Windows as anything requiring more than one worker process needs a POSIX system (Linux or macOS).

If you want to benchmark against Redis or Memcached instead of the default in-memory backend, start real instances first:

```bash
docker compose up -d redis memcached
```

Any backend other than `inmemory` or `multiprocess` needs a real, reachable server. The suite does not stub these out.

---

## Running Benchmarks

The suite is a `click`-based CLI with four commands, one per integration pattern:

```bash
python -m benchmarks http
python -m benchmarks middleware
python -m benchmarks websocket
python -m benchmarks multiprocess
```

Or via the Makefile shortcut, which forwards any arguments after `bench`:

```bash
make bench "http --scenarios below_limit,over_limit"
```

Each command accepts `--help` for the full option reference.

### Common Options

These options are shared across all four commands:

| Option | Short | Default | Description |
| --- | --- | --- | --- |
| `--backend` | `-b` | `inmemory` | Backend to benchmark. See [Backends](#backends) below. |
| `--strategy` | `-s` | `fixed_window` | Throttling strategy to benchmark. See [Strategies](#strategies) below. |
| `--iterations` | `-n` | `3` | Number of timed iterations per scenario. |
| `--warmup` | `-w` | `1` | Number of warmup iterations run (and discarded) before timing starts. |
| `--concurrency` | `-c` | `50` | Requests per batch in scenarios that send concurrent traffic. |
| `--workers` | `-W` | `1` (`4` for `multiprocess`) | Number of real worker processes serving the app. `1` spawns a single `uvicorn` process. Greater than `1` spawns `gunicorn` with `--preload`, forking that many real worker processes (POSIX only). |
| `--output` | `-o` | `table` | `table` (rich-rendered) or `json`. |
| `--redis-url` | | `redis://localhost:6379/0` | Connection URL, used when `--backend` is `aioredis` or `coredis`. |
| `--memcached-host` | | `localhost` | Used when `--backend` is `aiomcache` or `emcache`. |
| `--memcached-port` | | `11211` | Used when `--backend` is `aiomcache` or `emcache`. |
| `--scenarios` | | `all` | Comma-separated scenario names, or `all`. |

!!! warning "Workers and the in-memory backend"
    `--workers` greater than `1` combined with `--backend inmemory` will print a warning and still run, but the result is not meaningful: each forked worker gets its own independent copy of in-memory state, so requests routed to different workers won't see each other's counters. Use `--backend multiprocess` (or an external backend like `aioredis`/`coredis`) if you want to see real throttling behaviour across multiple worker processes.

---

## Commands and Scenarios

### `http` - Dependency-Based Throttling

Benchmarks the most common integration pattern: a throttle injected via `Depends(throttle)` on a single endpoint.

| Scenario | What it simulates |
| --- | --- |
| `below_limit` | Steady traffic comfortably under the configured rate. |
| `at_limit` | Traffic that lands exactly on the configured rate. |
| `over_limit` | Sustained traffic well past the limit, to measure rejection behaviour. |
| `concurrent` | A burst of concurrent requests all sharing one identity, to measure lock contention on a single key. |
| `hot_key` | Concurrent requests explicitly pinned to one `X-Client-ID`, similar intent to `concurrent` but with an explicit identity header. |
| `many_keys` | Concurrent requests spread across many distinct identities, to measure overhead when load is not concentrated on one key. |
| `window_boundary` | Bursts timed around fixed-window boundaries, to observe behaviour as a window resets. |
| `sustained` | A large, high-throughput burst against a generous limit, to measure best-case throughput. |
| `error_recovery` | Traffic against a throttle configured with `on_error="allow"`, to measure the fail-open path. |

### `middleware` - Middleware-Based Throttling

Benchmarks `ThrottleMiddleware` with a `MiddlewareThrottle` entry, applied without touching route handlers. Includes the same nine scenarios as `http`, plus:

| Scenario | What it simulates |
| --- | --- |
| `selective` | Traffic split between a throttled path and an exempt path, to confirm exempt routes pay no throttle cost and throttled routes are still enforced correctly. |

!!! note "`concurrent` differs from the `http` version"
    In `middleware`, the `concurrent` scenario round-robins requests across `--concurrency` distinct identities rather than hammering a single shared one. This is a deliberately different contention pattern from the `http` command's `concurrent` scenario. It's testing overhead under many simultaneously-active keys, not lock contention on one key.

### `websocket` - WebSocket Throttling

Benchmarks a single throttled `/ws` endpoint over real WebSocket connections.

| Scenario | What it simulates |
| --- | --- |
| `below_limit` | A steady stream of messages under the limit. |
| `over_limit` | A steady stream of messages well past the limit. |
| `burst` | A large burst of messages against a tight limit. |
| `concurrent` | Multiple simultaneous WebSocket connections, each sending a stream of messages. |
| `window_boundary` | Message bursts timed around fixed-window boundaries. |

### `multiprocess` - Real Multi-Worker State Sharing

Benchmarks `MultiProcessInMemoryBackend` across real, forked `gunicorn` workers (POSIX only). This command forces `--backend multiprocess` regardless of what `--backend` is passed, and reuses the same `Depends`-based endpoint as `http`. It includes the same nine scenarios as `http`, plus two that specifically stress the shared-memory backend:

| Scenario | What it simulates |
| --- | --- |
| `shared_memory` | A large concurrent burst, to stress the shared-memory segment under load spread across workers. |
| `key_eviction` | Many distinct keys sent in two waves with a pause between them, to observe key cleanup/eviction behaviour over time. |

!!! warning "`--workers` below 2"
    Running `multiprocess` with `--workers` set below `2` prints a warning: gunicorn won't actually fork multiple workers, so the run won't exercise any cross-process state sharing. Set `--workers` to at least `2` (and realistically, to your CPU core count) to test what this command is for.

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

## Reading the Output

### Table Output (default)

A `rich`-rendered table, one row per scenario, aggregated across all timed iterations (warmup iterations are discarded and never shown). Columns:

| Column | Meaning |
| --- | --- |
| Scenario | Scenario display name. |
| Backend / Strategy | What was benchmarked. |
| Requests | Total requests sent across all timed iterations. |
| RPS | Mean requests per second across iterations. |
| P50 / P95 / P99 | Latency percentiles, in milliseconds, pooled across all timed iterations. |
| Success % | Percentage of requests that received a `200`. |
| Throttled % | Percentage of requests that received a `429`. |
| Error % | Percentage of requests that failed for any other reason (connection errors, timeouts, unexpected status codes). |

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
      "mean_ms": 0.0,
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

**Success/throttle percentages should match the configured rate.** For a scenario sending `N` requests against a rate that permits `M` of them, expect roughly `M/N × 100` success and the rest throttled (barring the "many distinct keys" scenarios, where each key individually stays under its own limit and everything should succeed). If these don't line up, something's wrong with the run, not with your expectations.

**Numbers reflect real network and process overhead - by design.** Because this suite drives real HTTP/WebSocket traffic against a real server process, every request pays for a real TCP round trip, real HTTP/1.1 framing, and real ASGI request handling. That overhead did not exist in earlier versions of this suite (which called the ASGI app directly in-process) and won't disappear here - it's an accurate reflection of what a deployed instance actually costs per request, not a regression.

**Concurrency and `--workers` only pay off with real CPU cores.** `asyncio` concurrency helps most when there's real I/O wait time to overlap; on loopback that wait time is minimal, so a single worker process is largely CPU-bound on request parsing and routing. Multiple `gunicorn` workers only run in true parallel if there are separate physical cores for them to run on, i.e, on a single-core machine, `--workers 4` will look barely different from `--workers 1`, because there's only one core for either to use. If you want to see `--workers` make a real difference, set it based on how many cores your machine actually has and compare against a `--workers 1` run of the same scenario.

**`window_boundary` scenarios can show some run-to-run variance.** These scenarios time bursts around fixed-window edges, and real request latency plus real sleep timing can shift exactly where a burst lands relative to a window boundary. This is a genuine property of testing against real wall-clock timing, not a flaw in the scenario.

**Warmup iterations are discarded on purpose.** The first iteration against a freshly-started process can be slower (import caches warming, initial connection setup); warmup iterations exist to absorb that before timed iterations begin. Increase `--warmup` if you still see a slow first timed iteration.

---

## Troubleshooting

**`ERROR: Could not start server for <scenario>`**: the spawned `uvicorn`/`gunicorn` process failed its health check within the startup timeout. The error includes a tail of the process's stderr; check it first. Common causes: a missing dependency for the selected backend, or a backend that requires a running external server (Redis/Memcached) that isn't reachable.

**`--workers` greater than `1` fails outright**: this requires a POSIX system. `gunicorn`'s worker model relies on the `fork` start method, which Windows does not support. The `multiprocess` command is unavailable on Windows entirely for the same reason.

**Connection refused for `aioredis`/`coredis`/`aiomcache`/`emcache`**: start the relevant service first (`docker compose up -d redis memcached`), or point `--redis-url` / `--memcached-host` / `--memcached-port` at a server that's actually running.

**A scenario reports a nonzero error rate**: this means requests failed for a reason other than throttling (connection errors, timeouts, unexpected responses). It shouldn't happen in a healthy run; check the scenario's stderr output and the backend you selected.

---

## Extending the Suite

Scenarios are declarative specs, not hand-written functions. See `benchmarks/scenarios.py`. Adding a new scenario to an existing command means adding an entry to the relevant registry (`HTTP_SCENARIOS`, `MIDDLEWARE_SCENARIOS`, `WEBSOCKET_SCENARIOS`, or `MULTIPROCESS_SCENARIOS`) with a rate, request count, and traffic pattern (`sequential`, `concurrent`, `waves`, `unique_keys_batched`, `unique_keys_split`, or `mixed_paths` for HTTP-like scenarios). The actual traffic-generation logic lives in `benchmarks/live/runners.py` and is shared across every scenario of that shape. You shouldn't need to touch it to add a new scenario, only to add a genuinely new traffic pattern.
