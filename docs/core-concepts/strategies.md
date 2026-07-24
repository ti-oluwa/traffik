# Strategies

A strategy is the algorithm that decides **"how Traffik counts'**. Every strategy
receives the same inputs - a key, a rate, a backend, and the cost, and returns a wait time in
milliseconds. Zero means "go ahead". Anything positive means "slow down".

Choosing a strategy is a trade-off between accuracy, memory, and burst tolerance.
The table below gives you the overview; the sections that follow go deeper.

---

## Strategy comparison

| Strategy               | Accuracy | Memory    | Bursts             | Best For                                  |
|------------------------|----------|-----------|--------------------|-------------------------------------------|
| `FixedWindow`          | Low      | O(1)      | Yes (boundary)     | Simple limits, high throughput APIs       |
| `SlidingWindowCounter` | Medium   | O(1)      | Minimal            | General purpose, good default             |
| `SlidingWindowLog`     | Highest  | O(limit)  | No                 | Financial, security-critical, strict SLA  |
| `TokenBucket`          | High     | O(1)      | Yes (configurable) | Variable traffic, mobile clients          |
| `TokenBucketWithDebt`  | High     | O(1)      | Yes + overdraft    | Gradual degradation, user-facing APIs     |
| `LeakyBucket`          | High     | O(1)      | No                 | Protecting downstream services            |
| `LeakyBucketWithQueue` | High     | O(limit)  | No (strict FIFO)   | Ordered processing, fairness guarantees   |
| `GCRA`                 | Highest  | O(1)      | Configurable       | Telecom, smooth pipelines, strict SLA     |

Six more, built for narrower problems, live in `traffik.strategies.custom` - see [Advanced strategies](#advanced-strategies) below.

---

## `FixedWindow` (default)

This is the simplest possible algorithm. Time is divided into fixed windows aligned to clock
boundaries (e.g., 00:00–01:00, 01:00–02:00). Each request increments a counter for
the current window. When the window ends, the counter resets automatically via TTL.

**The boundary problem:** A user can make up to `limit` requests at 00:59 and another
`limit` at 01:00, up to 2x the limit within any two-second span. If that is
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

- `{namespace}:{key}:fixedwindow:counter` - request counter (integer, TTL = window duration)
- `{namespace}:{key}:fixedwindow:start` - window start timestamp (sub-second windows only)

!!! tip "`FixedWindow` is the default"
    You do not need to pass `strategy=FixedWindow()` explicitly, it is what you get
    when you omit the `strategy` argument entirely.

---

## `SlidingWindowCounter`

A smarter cousin of `FixedWindow`. Instead of snapping to fixed clock boundaries,
it tracks two consecutive windows and computes a weighted count:

```text
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

- `{namespace}:{key}:slidingcounter:{window_id}` - plain integer counter per window (TTL = 2x window duration)
- Two consecutive windows are read on every request.

**When to use:** General-purpose rate limiting where you want better accuracy than `FixedWindow` without paying the memory cost of `SlidingWindowLog`.

---

## `SlidingWindowLog`

This is the most accurate strategy. Traffik maintains a log of `(timestamp, cost)` pairs for
every request in the window. On each request it evicts expired entries and sums the
remaining costs.

```python
from traffik.strategies import SlidingWindowLog

throttle = HTTPThrottle(
    uid="payment-endpoint",
    rate="100/min",
    strategy=SlidingWindowLog(),
)
```

**Storage keys:**

- `{namespace}:{key}:slidinglog` - a struct-packed list of `(timestamp_ms, cost)` records

**Memory:** Is O(limit). At peak load the log holds one entry per allowed request.
With a limit of 10,000 this can add up. Choose `SlidingWindowCounter` if memory is a
concern.

!!! warning "Use `SlidingWindowLog` only when accuracy is mandatory"
    For most APIs, `SlidingWindowCounter` gives 95% of the accuracy at a fraction of
    the memory. Reserve `SlidingWindowLog` for payment processing, security-critical
    endpoints, and anywhere where the boundary burst of `FixedWindow` is genuinely
    unacceptable.

---

## `TokenBucket`

This strategy models rate limiting as a bucket that holds tokens. Tokens refill at a constant rate
(e.g., `100 tokens/minute`). Each request consumes one token (or `cost` tokens). If
the bucket is empty, the request must wait until enough tokens have accumulated.

The `burst_size` parameter controls the maximum bucket capacity (maximum number of tokens that the bucket can hold idle). Set it higher than `rate.limit` to allow occasional bursts; set it equal to `rate.limit` for no extra burst allowance.

```python
from traffik.strategies import TokenBucket

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    strategy=TokenBucket(burst_size=150),  # allow bursts up to 150
)
```

**Storage keys:**

- `{namespace}:{key}:tokenbucket:{capacity}` - struct-packed `(tokens: float, last_refill_ms: float)`

**Refill formula:** `new_tokens = min(current + elapsed_ms * (limit / expire_ms), capacity)`

Tokens are refilled lazily on every request, there is no background process.

---

## `TokenBucketWithDebt`

This just an extended token bucket that lets the bucket go **negative**. Requests are still
allowed when the bucket is at zero, they go into "debt" up to `max_debt`. The debt
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

- `{namespace}:{key}:tokenbucket:{capacity}:debt:{max_debt}` - same struct-packed `(tokens, last_refill_ms)` format as `TokenBucket`, but tokens can be negative

**When not to use:** Strict SLA enforcement, third-party API proxying, billing and
payment systems. Debt means you can temporarily exceed the nominal rate.

---

## `LeakyBucket`

This is the inverse of `TokenBucket`. Instead of tokens filling a bucket, requests fill it,
and it drains (leaks) at a constant rate. If the bucket is full when a new request
arrives, that request is rejected.

The effect is perfectly smooth output, no bursts are ever allowed. This is ideal for
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

- `{namespace}:{key}:leakybucket:state` - struct-packed `(level: float, last_leak_ms: float)`

---

## `LeakyBucketWithQueue`

`LeakyBucket` with a strict FIFO guarantee. Instead of a simple fill level, Traffik
stores the full queue of `(timestamp, cost)` entries. Requests drain in the exact
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

- `{namespace}:{key}:leakybucketqueue:state` - struct-packed `(queue: list[(timestamp, cost)], last_leak_ms: float)`

**Memory:** O(limit) for the same reason as `SlidingWindowLog`.

---

## `GCRA`

The **Generic Cell Rate Algorithm**, also called "leaky bucket as a meter" or
"virtual scheduling". Originally designed for ATM networks, it provides the smoothest
possible rate enforcement with the smallest memory footprint of any stateful
algorithm: a single float (the Theoretical Arrival Time, TAT).

**How it works:** Every request is only allowed if `now >= TAT - burst_tolerance_ms`.
After an allowed request, the TAT advances by one emission interval
(`window_duration / limit`). The result is that requests are uniformly spaced in time.

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

- `{namespace}:{key}:gcra:tat` - a single float, the TAT in milliseconds, stored as a plain string

**When to use:** Telecommunications systems, real-time data pipelines, strict SLA
contracts, anywhere you need requests to be evenly spread over time rather than
clustered at the start of a window.

!!! tip "GCRA is the most memory-efficient accurate strategy"
    Just one float per key. If you need accuracy and have a large number of unique
    keys (millions of users), GCRA beats `SlidingWindowLog` decisively on memory.

---

## Advanced strategies

Six more strategies live in `traffik.strategies.custom`, built for specific,
narrower problems rather than general-purpose limiting. They're not drop-in
replacements for the eight above in the same way some of those are for each other -
each one encodes an actual policy decision (how tiers are weighted, how priority is
broken, how quota rolls over), so read the one you're reaching for before using it.
Every one still implements the same strategy interface, so it plugs into
`HTTPThrottle`/`WebSocketThrottle` exactly like any core strategy.

```python
from traffik.strategies.custom import (
    TieredRate,
    AdaptiveThrottle,
    PriorityQueue,
    QuotaWithRollover,
    TimeOfDay,
    CostBasedTokenBucket,
)
```

Each is also available under its full name (`TieredRateStrategy`, `AdaptiveThrottleStrategy`, etc.) if you'd rather be explicit at the import site.

---

### `TieredRate`

Fixed-window limiting where the effective rate depends on a tier baked into the
throttle key - "free"/"premium"/"enterprise", or any tiering scheme you want.

**How it works:** the key must contain a marker segment (`"tier:"` by default) followed by the tier name - `"tier:premium:user:123"`. The strategy extracts the tier, looks up its multiplier, and applies it to the base rate for that window.

```python
from traffik.strategies.custom import TieredRate

# Base rate: 100/hour. Free: 100/hour, Premium: 500/hour, Enterprise: 1000/hour.
strategy = TieredRate(
    tier_multipliers={"free": 1.0, "premium": 5.0, "enterprise": 10.0},
    default_tier="free",
)

async def tier_identifier(connection):
    user = extract_user(connection)
    return f"tier:{user.tier}:user:{user.id}"

throttle = HTTPThrottle(
    uid="api",
    rate="100/hour",  # base rate for free tier
    strategy=strategy,
    identifier=tier_identifier,
)
```

**Storage keys:** `{namespace}:{key}:tiered:{tier}:{window}` - counter per tier per window

**When to use:** SaaS APIs with tiered pricing plans, where the limit itself (not just access) should scale with what a customer's paying for.

---

### `AdaptiveThrottle`

Rate limiting that tightens automatically as load approaches capacity, and eases back off as it recovers. It acts as a shock absorber sitting in front of your real limit.

**How it works:** tracks current load as a percentage of the limit. Once load crosses `load_threshold`, the effective limit is reduced to `reduction_factor` of normal (but never below `min_limit_ratio`); as load drops, the limit recovers gradually at `recovery_rate` per window rather than snapping back immediately.

```python
from traffik.strategies.custom import AdaptiveThrottle

strategy = AdaptiveThrottle(
    load_threshold=0.8,     # start throttling at 80% capacity
    reduction_factor=0.6,   # reduce to 60% of normal limit
    recovery_rate=0.1,      # recover 10% per window
)

throttle = HTTPThrottle(
    uid="adaptive_api",
    rate="1000/hour",  # can adapt down to ~600/hour under load
    strategy=strategy,
)
```

**Storage keys:**

- `{namespace}:{key}:adaptive:{window}:counter` - request counter
- `{namespace}:{key}:adaptive:{window}:limit` - current effective limit

**When to use:** protecting a backend from thundering-herd traffic without a hard cutoff - smooths spikes instead of just rejecting everything the moment you hit the wall.

---

### `PriorityQueue`

Rate limiting where requests are triaged by priority under contention. Critical/admin traffic gets through before low-priority batch jobs do, at the same nominal limit.

**How it works:** maintains a queue of `(timestamp, priority, cost)` entries. Higher-priority entries are considered first when checking capacity, so low-priority requests are the ones that end up waiting when the limit is tight. Priority is read from a marker segment in the key (`"priority:"` by default), same mechanism as `TieredRate`'s tier marker.

```python
from traffik.strategies.custom import PriorityQueue, Priority

strategy = PriorityQueue(
    default_priority=Priority.NORMAL,
    max_queue_size=1000,  # bound memory growth
)

async def priority_identifier(connection):
    user = extract_user(connection)
    priority = connection.headers.get("x-priority", "2")
    return f"priority:{priority}:user:{user.id}"

throttle = HTTPThrottle(
    uid="priority_api",
    rate="100/minute",
    strategy=strategy,
    identifier=priority_identifier,
)
```

`Priority` is an `IntEnum`: `LOW = 1`, `NORMAL = 2`, `HIGH = 3`, `CRITICAL = 4`.

**Storage keys:** `{namespace}:{key}:priority:queue` - struct-packed list of `(timestamp, priority, cost)` records

**When to use:** admin/system calls that must go through regardless of load, paid-tier requests that should be favored over free-tier ones, or keeping interactive traffic responsive while background jobs absorb the throttling.

---

### `QuotaWithRollover`

Period-based quotas (monthly API calls, credits, subscription limits) where unused quota isn't just wasted at the period boundary.

**How it works:** tracks usage for the current period against the base rate. At the end of a period, up to `rollover_percentage` of what went unused (capped at `max_rollover`) carries into the next period's budget.

```python
from traffik.strategies.custom import QuotaWithRollover

strategy = QuotaWithRollover(
    rollover_percentage=0.5,  # roll over 50% of unused quota
    max_rollover=500,          # cap rollover at 500 requests
)

throttle = HTTPThrottle(
    uid="monthly_quota",
    rate="1000/30days",  # 1000 per month
    strategy=strategy,
)
```

**Storage keys:**

- `{namespace}:{key}:quota:{period}:usage` - used quota this period (plain integer)
- `{namespace}:{key}:quota:{period}:rollover` - rolled over from the previous period

**When to use:** billing-cycle-aligned API quotas, credit systems, subscription plans - anywhere "use it or lose it" would actively annoy your users.

---

### `TimeOfDay`

Fixed-window limiting where the multiplier applied to the base rate depends on the hour of day - peak/off-peak pricing, or business-hours enforcement.

**How it works:** you define a list of `(start_hour, end_hour, multiplier)` windows in 24-hour UTC-relative time (adjustable via `timezone_offset`). Windows should be contiguous and cover the full 24 hours; the current hour picks which multiplier applies.

```python
from traffik.strategies.custom import TimeOfDay

strategy = TimeOfDay(
    time_windows=[
        (0, 6, 2.0),    # night: 2x limit (200/hour)
        (6, 18, 1.0),   # day: 1x limit (100/hour)
        (18, 24, 1.5),  # evening: 1.5x limit (150/hour)
    ],
    timezone_offset=0,  # UTC offset in hours, -12 to +14
)

throttle = HTTPThrottle(
    uid="tod_api",
    rate="100/hour",  # base rate
    strategy=strategy,
)
```

!!! note "Window boundaries"
    Each `(start_hour, end_hour)` window is inclusive of `start_hour` and exclusive of `end_hour`.

**Storage keys:** `{namespace}:{key}:tod:{window_id}:counter` - counter per time window

**When to use:** off-peak incentives, throttling batch/reporting endpoints harder during business hours to protect interactive traffic, or the inverse.

---

### `CostBasedTokenBucket`

A token bucket where the refill rate itself adapts to how expensive recent requests have actually been, not just how many of them there were.

**How it works:** tracks the average `cost` of the last `cost_window` requests. When that average trends up (expensive operations dominating), the effective refill rate slows down temporarily, never below `min_refill_rate` of nominal; it recovers as the average cost drops.

```python
from traffik.strategies.custom import CostBasedTokenBucket

strategy = CostBasedTokenBucket(
    burst_size=200,
    cost_window=100,  # average over the last 100 requests
)

throttle = HTTPThrottle(
    uid="cost_api",
    rate="100/minute",
    strategy=strategy,
)

# Usage with per-call costs:
await throttle(request, cost=1)   # simple read
await throttle(request, cost=10)  # complex query
await throttle(request, cost=50)  # report generation
```

**Storage keys:**

- `{namespace}:{key}:costbucket:state` - struct-packed `(tokens: float, last_refill_ms: float)`
- `{namespace}:{key}:costbucket:history` - struct-packed list of recent costs, used for the running average

**When to use:** APIs where operation cost genuinely varies a lot (a health check vs. a report export) and you want the bucket to account for that instead of treating every request as equally cheap.

---

## Custom strategies

If none of the fourteen above fit your problem? Strategies are just callables with a small, well-defined interface - see [Extending Traffik: Custom Strategies](../extending/strategies.md) for how to write your own.
