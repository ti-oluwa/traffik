# Rates

A `Rate` answers one question: **how many requests are allowed in how much time?**

Traffik gives you two ways to express that: a human-friendly string, or the `Rate`
object itself. Both end up in exactly the same place.

---

## String format

The string format is the fastest path to a working rate limit. Traffik parses it
once, caches the result (LRU, capacity 512), and never touches it again.

```python
rate = "100/min"     # 100 requests per minute
rate = "5/s"         # 5 per second
rate = "10/10s"      # 10 per 10 seconds
rate = "2 per second"
rate = "500/hour"
rate = "1000/500ms"  # sub-second windows work too
```

The general grammar is:

```
<limit>/<period><unit>
<limit> per <period><unit>
<limit>/<period> <unit>
```

Where `<period>` is an optional integer multiplier (defaults to `1`) and `<unit>` is
one of the values in the table below.

### Supported units

| Unit string(s)                      | Meaning       |
|-------------------------------------|---------------|
| `ms`, `millisecond`, `milliseconds` | Milliseconds  |
| `s`, `sec`, `second`, `seconds`     | Seconds       |
| `m`, `min`, `minute`, `minutes`     | Minutes       |
| `h`, `hr`, `hour`, `hours`          | Hours         |
| `d`, `day`, `days`                  | Days          |

### Rate string reference

| String                  | Meaning                           |
|-------------------------|-----------------------------------|
| `"5/m"`                 | 5 requests per minute             |
| `"5/min"`               | 5 requests per minute             |
| `"5 per minute"`        | 5 requests per minute             |
| `"100/h"`               | 100 requests per hour             |
| `"100/hour"`            | 100 requests per hour             |
| `"10 per second"`       | 10 requests per second            |
| `"2/5s"`                | 2 requests per 5 seconds          |
| `"10/30 seconds"`       | 10 requests per 30 seconds        |
| `"1000/500ms"`          | 1000 requests per 500 ms          |
| `"50/d"`                | 50 requests per day               |
| `"0/0"`                 | Unlimited (no throttling)         |

!!! tip "Stick to strings for simple limits"
    For everyday limits like "100 per minute" or "5 per second", the string form
    is cleaner and immediately readable at a glance. Reserve the `Rate` object for
    periods that don't map to a single unit.

---

## The `Rate` object

When you need a period that spans multiple units — say, 5 minutes and 30 seconds —
or when you want to construct a rate programmatically, use `Rate` directly.

```python
from traffik.rates import Rate

# Simple: 100 requests per minute
rate = Rate(limit=100, minutes=1)

# Combined units: 100 requests per 5 minutes and 30 seconds
rate = Rate(limit=100, minutes=5, seconds=30)

# Sub-second: 50 requests per 500 milliseconds
rate = Rate(limit=50, milliseconds=500)
```

`Rate` accepts the following keyword arguments (all default to `0`):

| Parameter      | Unit         |
|----------------|--------------|
| `limit`        | Request count |
| `milliseconds` | ms           |
| `seconds`      | s            |
| `minutes`      | min          |
| `hours`        | h            |

All time parameters are additive — the expire period is their sum in milliseconds.

`Rate` objects are **immutable** and **final** (subclassing is forbidden). Once
created, every attribute — `limit`, `expire`, `rps`, `rpm`, `rph`, `rpd`,
`is_subsecond`, `unlimited` — is frozen.

!!! warning "Both limit and period must be set together"
    Passing `limit=100` without any time unit raises `ValueError`. Passing
    `milliseconds=500` without `limit` also raises `ValueError`. Both must be
    non-zero, or both must be zero (unlimited).

---

## Parsing with `Rate.parse()`

Under the hood, string rates go through `Rate.parse()`, which wraps an LRU-cached
internal function with a capacity of 512 entries. This means the thousandth time you
hit `"100/min"`, the parse cost is effectively zero.

```python
rate = Rate.parse("100/min")
rate = Rate.parse("2/5s")
rate = Rate.parse("10 per second")
```

Parsing is case-insensitive. `"100/MIN"` and `"100/min"` hit the same cache entry.

---

## Dynamic rate functions

Sometimes the right rate limit depends on the request itself. Maybe free users get
`"10/min"` and pro users get `"1000/min"`. Traffik supports **async rate functions**
that receive the current connection and context, and return a `Rate`.

```python
from traffik.rates import Rate
from starlette.requests import Request

async def my_rate(connection: Request, context: dict | None) -> Rate:
    user = connection.state.user
    if user and user.plan == "pro":
        return Rate.parse("1000/min")
    return Rate.parse("10/min")
```

Pass the function anywhere a rate is accepted:

```python
from traffik import HTTPThrottle

throttle = HTTPThrottle(
    uid="dynamic-rate",
    rate=my_rate,  # async function, not a string or Rate
)
```

!!! tip "Dynamic rates are called on every request"
    Unlike static strings or `Rate` objects, dynamic functions are called on every
    request. Keep them fast — a database lookup per request is a latency hit on
    every single call.

---

## Unlimited rates

To explicitly mark something as unlimited (no throttling at all), use either:

```python
Rate()                # no arguments → unlimited
Rate.parse("0/0")    # string form
```

Both produce `Rate(unlimited=True)` with `rate.unlimited == True`. Traffik's
strategies detect this flag and short-circuit immediately — zero backend I/O, zero
overhead.

!!! tip "Use unlimited for internal health check endpoints"
    A health check that hits the same throttle as your main API can skew your
    counters. Give it an unlimited rate (or return `EXEMPTED` from the identifier)
    and keep your metrics clean.
