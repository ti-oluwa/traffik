# Strategies

A strategy is the algorithm that decides **how Traffik counts**. Every strategy
receives the same inputs — a key, a rate, and a backend — and returns a wait time in
milliseconds. Zero means "go ahead". Anything positive means "slow down".

Choosing a strategy is a trade-off between accuracy, memory, and burst tolerance.
The table below gives you the overview; the sections that follow go deeper.

---

## Strategy comparison

| Strategy                  | Accuracy | Memory       | Bursts           | Best For                                 |
|---------------------------|----------|--------------|------------------|------------------------------------------|
| `FixedWindow`             | Low      | O(1)         | Yes (boundary)   | Simple limits, high throughput APIs      |
| `SlidingWindowCounter`    | Medium   | O(1)         | Minimal          | General purpose, good default            |
| `SlidingWindowLog`        | Highest  | O(limit)     | No               | Financial, security-critical, strict SLA |
| `TokenBucket`             | High     | O(1)         | Yes (configurable) | Variable traffic, mobile clients       |
| `TokenBucketWithDebt`     | High     | O(1)         | Yes + overdraft  | Gradual degradation, user-facing APIs    |
| `LeakyBucket`             | High     | O(1)         | No               | Protecting downstream services           |
| `LeakyBucketWithQueue`    | High     | O(limit)     | No (strict FIFO) | Ordered processing, fairness guarantees  |
| `GCRA`                    | Highest  | O(1)         | Configurable     | Telecom, smooth pipelines, strict SLA    |

---

## FixedWindow (default)

The simplest possible algorithm. Time is divided into fixed windows aligned to clock
boundaries (e.g., 00:00–01:00, 01:00–02:00). Each request increments a counter for
the current window. When the window ends, the counter resets automatically via TTL.

**The boundary problem:** A user can make up to `limit` requests at 00:59 and another
`limit` at 01:00 — up to 2x the limit within any two-second span. If that is
acceptable, `FixedWindow` is your best friend: it is fast, cheap, and requires only
one atomic counter per key.

```python
from traffik import HTTPThrottle
from traffik.strategies import FixedWindow

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    strategy=FixedWindow(),  # this is also the default if you omit strategy
)
```

**Storage keys:**

- `{namespace}:{key}:fixedwindow:counter` — request counter (integer, TTL = window duration)
- `{namespace}:{key}:fixedwindow:start` — window start timestamp (sub-second windows only)

!!! tip "FixedWindow is the default"
    You do not need to pass `strategy=FixedWindow()` explicitly — it is what you get
    when you omit the `strategy` argument entirely.

---

## SlidingWindowCounter

A smarter cousin of `FixedWindow`. Instead of snapping to fixed clock boundaries,
it tracks two consecutive windows and computes a weighted count:

```
weighted_count = (previous_count * overlap_percentage) + current_count
```

As you move further through the current window, the previous window's contribution
shrinks toward zero. The result is much smoother traffic flow with only two counters
per key.

```python
from traffik.strategies import SlidingWindowCounter

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    strategy=SlidingWindowCounter(),
)
```

**Storage keys:**

- `{namespace}:{key}:slidingcounter:{window_id}` — counter per window (TTL = 2x window duration)
- Two consecutive windows are read on every request.

**When to use:** General-purpose rate limiting where you want better accuracy than
`FixedWindow` without paying the memory cost of `SlidingWindowLog`.

---

## SlidingWindowLog

The most accurate strategy. Traffik maintains a log of `[timestamp, cost]` pairs for
every request in the window, serialised with MessagePack. On each request it evicts
expired entries and sums the remaining costs.

```python
from traffik.strategies import SlidingWindowLog

throttle = HTTPThrottle(
    uid="payment-endpoint",
    rate="100/min",
    strategy=SlidingWindowLog(),
)
```

**Storage keys:**

- `{namespace}:{key}:slidinglog` — msgpack-encoded list of `[timestamp_ms, cost]` tuples

**Memory:** O(limit) — at peak load the log holds one entry per allowed request.
With a limit of 10,000 this can add up. Choose `SlidingWindowCounter` if memory is a
concern.

!!! warning "Use SlidingWindowLog only when accuracy is mandatory"
    For most APIs, `SlidingWindowCounter` gives 95% of the accuracy at a fraction of
    the memory. Reserve `SlidingWindowLog` for payment processing, security-critical
    endpoints, and anywhere where the boundary burst of `FixedWindow` is genuinely
    unacceptable.

---

## TokenBucket

Models rate limiting as a bucket that holds tokens. Tokens refill at a constant rate
(e.g., `100 tokens/minute`). Each request consumes one token (or `cost` tokens). If
the bucket is empty, the request must wait until enough tokens have accumulated.

The `burst_size` parameter controls the maximum bucket capacity. Set it higher than
`rate.limit` to allow occasional bursts; set it equal to `rate.limit` for no extra
burst allowance.

```python
from traffik.strategies import TokenBucket

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    strategy=TokenBucket(burst_size=150),  # allow bursts up to 150
)
```

**Storage keys:**

- `{namespace}:{key}:tokenbucket:{capacity}` — msgpack `{"tokens": float, "last_refill": ms}`

**Refill formula:** `new_tokens = min(current + elapsed_ms * (limit / expire_ms), capacity)`

Tokens are refilled lazily on every request — there is no background process.

---

## TokenBucketWithDebt

An extended token bucket that lets the bucket go **negative**. Requests are still
allowed when the bucket is at zero — they go into "debt" up to `max_debt`. The debt
is paid back through normal token refilling. This produces a softer experience: users
never hit a sudden wall; traffic degrades gradually.

```python
from traffik.strategies import TokenBucketWithDebt

throttle = HTTPThrottle(
    uid="user-facing-api",
    rate="100/min",
    strategy=TokenBucketWithDebt(
        burst_size=150,  # max positive tokens
        max_debt=50,     # allow up to 50 tokens of debt
    ),
)
```

**Storage keys:**

- `{namespace}:{key}:tokenbucket:{capacity}:debt:{max_debt}` — same state format as `TokenBucket`, but tokens can be negative

**When NOT to use:** Strict SLA enforcement, third-party API proxying, billing and
payment systems. Debt means you can temporarily exceed the nominal rate.

---

## LeakyBucket

The inverse of `TokenBucket`. Instead of tokens filling a bucket, requests fill it,
and it drains (leaks) at a constant rate. If the bucket is full when a new request
arrives, that request is rejected.

The effect is perfectly smooth output — no bursts are ever allowed. This is ideal for
protecting downstream services that cannot handle spikes.

```python
from traffik.strategies import LeakyBucket

throttle = HTTPThrottle(
    uid="downstream-proxy",
    rate="100/min",
    strategy=LeakyBucket(),
)
```

**Storage keys:**

- `{namespace}:{key}:leakybucket:state` — msgpack `{"level": float, "last_leak": ms}`

---

## LeakyBucketWithQueue

`LeakyBucket` with a strict FIFO guarantee. Instead of a simple fill level, Traffik
stores the full queue of `[timestamp, cost]` entries. Requests drain in the exact
order they arrived. No request can "cut in line".

```python
from traffik.strategies import LeakyBucketWithQueue

throttle = HTTPThrottle(
    uid="ordered-queue",
    rate="100/min",
    strategy=LeakyBucketWithQueue(),
)
```

**Storage keys:**

- `{namespace}:{key}:leakybucketqueue:state` — msgpack `{"queue": [[ts, cost], ...], "last_leak": ms}`

**Memory:** O(limit) for the same reason as `SlidingWindowLog`.

---

## GCRA

The **Generic Cell Rate Algorithm** — also called "leaky bucket as a meter" or
"virtual scheduling". Originally designed for ATM networks, it provides the smoothest
possible rate enforcement with the smallest memory footprint of any stateful
algorithm: a single float (the Theoretical Arrival Time, TAT).

**How it works:** Every request is only allowed if `now >= TAT - burst_tolerance_ms`.
After an allowed request, the TAT advances by one emission interval
(`window_duration / limit`). The result: requests are uniformly spaced in time.

```python
from traffik.strategies.custom import GCRA

# Perfectly smooth: 100 req/min = one request every 600ms
throttle = HTTPThrottle(
    uid="realtime-pipeline",
    rate="100/min",
    strategy=GCRA(burst_tolerance_ms=0),  # 0 = no bursts at all
)

# Allow a small burst tolerance (e.g. 1 full emission interval)
throttle = HTTPThrottle(
    uid="realtime-pipeline",
    rate="100/min",
    strategy=GCRA(burst_tolerance_ms=600),  # one request's worth of tolerance
)
```

**Storage keys:**

- `{namespace}:{key}:gcra:tat` — a single float: the TAT in milliseconds

**When to use:** Telecommunications systems, real-time data pipelines, strict SLA
contracts, anywhere you need requests to be evenly spread over time rather than
clustered at the start of a window.

!!! tip "GCRA is the most memory-efficient accurate strategy"
    Just one float per key. If you need accuracy and have a large number of unique
    keys (millions of users), GCRA beats `SlidingWindowLog` decisively on memory.

---

## Advanced strategies in `traffik.strategies.custom`

Traffik ships a second, richer set of strategies for specialised scenarios. They live
in `traffik.strategies.custom` and are imported explicitly:

```python
from traffik.strategies.custom import (
    AdaptiveThrottle,   # dynamically reduces limits under high load
    TieredRate,         # different multipliers per user tier (free/pro/enterprise)
    PriorityQueue,      # CRITICAL / HIGH / NORMAL / LOW priority classes
    QuotaWithRollover,  # monthly quotas where unused quota carries over
    TimeOfDay,          # different limits by hour of day
    CostBasedTokenBucket, # refill rate adjusts based on average request cost
    DistributedFairness,  # deficit round-robin across multiple app instances
    GeographicDistribution, # per-region capacity with optional spillover
)
```

Each of these strategies is a drop-in replacement — same interface, same `__call__`
signature. Check the API reference and the `traffik.strategies.custom` module for
full configuration details and examples.
