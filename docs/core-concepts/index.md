# Core Concepts

Rate limiting in Traffik is built around four simple, composable ideas. Get these and you get everything else in these docs.

---

## The Four Building Blocks

### Rate

A **Rate** answers "how much traffic is too much?". It's a request limit paired with a time window: "100 requests per minute," "5 per second," "1000 per hour". Write it as a string (`"100/min"`) or build a `Rate` object directly for more specific periods (`Rate(limit=100, minutes=5, seconds=30)`).

[Rates in depth &rarr;](rates.md)

---

### Backend

A **Backend** is where Traffik keeps score. Every hit increments a counter (or equivalent), checks it against the limit, and hands back a verdict. Four backends come built in: `InMemoryBackend` for development and single-process apps, `MultiProcessInMemoryBackend` for sharing state across workers on one machine without Redis (experimental! Read the caveats before using it), `RedisBackend` for production and distributed systems, and `MemcachedBackend` for when Memcached is already in your stack.

[Backends in depth &rarr;](backends.md)

---

### Strategy

A **Strategy** is the algorithm deciding *how* to count. A simple fixed window that resets every minute? A sliding window that's immune to boundary bursts? A token bucket that allows occasional bursts on top of a sustained rate? Eight strategies come built in, from `FixedWindow` to `GCRA`, plus six more bespoke ones in `traffik.strategies.custom` for when a specific problem (per-tier limits, load-adaptive throttling, ...) genuinely needs for them.

[Strategies in depth &rarr;](strategies.md)

---

### Identifier

An **Identifier** is how Traffik knows who's who. By default Traffik identifies by the client's IP, but you can replace it with: a user ID pulled from a JWT, an API key from a header, a tenant slug from the URL. Return the `EXEMPTED` sentinel to let a specific client through untouched, no separate bypass logic needed.

[Identifiers in depth &rarr;](identifiers.md)

---

## Putting It Together

```python
from traffik import HTTPThrottle
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")

throttle = HTTPThrottle(
    uid="my-endpoint",
    rate="100/min",       # Rate
    backend=backend,      # Backend
    # strategy defaults to FixedWindow
    # identifier defaults to remote IP
)
```

> A `Throttle` is really just these four things bundled behind one callable.
