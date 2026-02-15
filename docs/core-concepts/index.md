# Core Concepts

Welcome to the heart of Traffik. Rate limiting can sound intimidating, but Traffik
is built around four simple, composable ideas. Master these and you master everything.

---

## The Four Building Blocks

### Rate

A **Rate** is the answer to "how much traffic is too much?" It bundles a request
limit together with a time window — "100 requests per minute", "5 per second", "1000
per hour". You can express it as a human-friendly string like `"100/min"` or
construct it as a `Rate` object when you need a more complex period.

[Rates in depth &rarr;](rates.md)

---

### Backend

A **Backend** is where Traffik keeps score. Every time a request comes in, the backend
increments a counter, checks whether the limit has been crossed, and hands back a
verdict. Traffik ships with three backends out of the box: an **InMemory** backend
for development and single-process apps, a **Redis** backend for production and
distributed systems, and a **Memcached** backend for when you already have Memcached
in your stack.

[Backends in depth &rarr;](backends.md)

---

### Strategy

A **Strategy** is the algorithm that decides *how* to count. Should Traffik use a
simple fixed window that resets every minute? A sliding window that's immune to
boundary bursts? A token bucket that allows occasional burst traffic? There are seven
strategies built in — from the dead-simple `FixedWindow` to the telecom-grade `GCRA`
— plus a whole collection of advanced strategies in `traffik.strategies.custom`.

[Strategies in depth &rarr;](strategies.md)

---

### Identifier

An **Identifier** is how Traffik knows who is who. By default it reads the client's
IP address, but you can swap in anything: a user ID extracted from a JWT, an API key
from a header, a tenant slug from the URL. You can even return the special `EXEMPTED`
sentinel to let a specific client sail straight through without touching the backend
at all.

[Identifiers in depth &rarr;](identifiers.md)

---

## Putting It Together

```python
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")

throttle = HTTPThrottle(
    uid="my-endpoint",
    rate="100/min",       # Rate
    backend=backend,      # Backend
    # strategy defaults to FixedWindow
    # identifier defaults to remote IP
)
```

Once you understand these four things — **Rate**, **Backend**, **Strategy**,
**Identifier** — you understand Traffik.
