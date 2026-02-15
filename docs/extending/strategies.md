# Building Custom Strategies

The built-in strategies cover the vast majority of rate limiting needs. But Traffik is built for extension, and writing your own strategy is surprisingly straightforward.

Maybe you need a strategy that accounts for user tier. Maybe you want a "quota with rollover" that carries unused tokens to the next window. Maybe you've discovered a new algorithm in a paper and want to try it. Whatever the reason, here's how.

---

## The Strategy Protocol

A strategy is any async callable that matches this signature:

```python
async def __call__(
    self,
    key: Stringable,        # The namespaced throttle key for this client
    rate: Rate,             # The rate limit definition
    backend: ThrottleBackend,  # The backend to read/write from
    cost: int = 1,          # How many units this request costs
) -> WaitPeriod:            # Milliseconds to wait (0 = allow, >0 = throttle)
```

That's the entire contract. Return `0.0` to allow the request. Return a positive number of milliseconds to throttle the client.

!!! tip "Add get_stat() for observability"
    If you also implement `get_stat(key, rate, backend) -> StrategyStat`, your strategy will support `throttle.stat()` calls and all the observability features that come with it. It's optional but highly recommended.

---

## Full Example: Sliding Quota with Priority

Here's a complete custom strategy that gives "priority" clients a higher limit than regular clients by reading a flag from the context.

Actually, a simpler and more useful example: a **ResetOnFirstHit** strategy that tracks when a client first used the API in a window, and gives them the full window from that point (rather than aligning to clock boundaries):

```python
from dataclasses import dataclass, field
from typing import Optional

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import time


@dataclass(frozen=True)
class RollingWindowStrategy:
    """
    A rolling window strategy that starts the window on the client's first request.

    Unlike FixedWindow (which aligns to clock boundaries), this gives each client
    a full 'rate.expire' milliseconds from their first request.
    """

    lock_config: LockConfig = field(default_factory=dict)

    async def __call__(
        self,
        key: Stringable,
        rate: Rate,
        backend: ThrottleBackend,
        cost: int = 1,
    ) -> WaitPeriod:
        # Always check for unlimited rate first — zero overhead fast path
        if rate.unlimited:
            return 0.0

        now_ms = time() * 1000
        full_key = backend.get_key(str(key))
        start_key = f"{full_key}:rolling:start"
        count_key = f"{full_key}:rolling:count"
        ttl_seconds = max(int(rate.expire // 1000), 1)

        # Multi-step: need a lock to prevent races
        async with await backend.lock(f"lock:{full_key}:rolling", **self.lock_config):
            window_start = await backend.get(start_key)

            if window_start is None:
                # First request from this client — start their window now
                await backend.multi_set(
                    {
                        start_key: str(now_ms),
                        count_key: str(cost),
                    },
                    expire=ttl_seconds,
                )
                return 0.0

            window_start_ms = float(window_start)
            window_end_ms = window_start_ms + rate.expire

            if now_ms >= window_end_ms:
                # Window expired — start a fresh one
                await backend.multi_set(
                    {
                        start_key: str(now_ms),
                        count_key: str(cost),
                    },
                    expire=ttl_seconds,
                )
                return 0.0

            # Inside the window — increment and check
            counter = await backend.increment_with_ttl(count_key, amount=cost, ttl=ttl_seconds)

        if counter > rate.limit:
            # Over the limit — tell client when their window resets
            wait_ms = window_end_ms - now_ms
            return max(wait_ms, 0.0)

        return 0.0

    async def get_stat(
        self,
        key: Stringable,
        rate: Rate,
        backend: ThrottleBackend,
    ) -> StrategyStat:
        now_ms = time() * 1000
        full_key = backend.get_key(str(key))
        start_key = f"{full_key}:rolling:start"
        count_key = f"{full_key}:rolling:count"

        window_start_raw, count_raw = await backend.multi_get(start_key, count_key)

        if window_start_raw is None:
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float(rate.limit),
                wait_ms=0.0,
            )

        window_start_ms = float(window_start_raw)
        window_end_ms = window_start_ms + rate.expire
        counter = int(count_raw) if count_raw else 0

        if now_ms >= window_end_ms:
            # Window expired — fresh slate
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float(rate.limit),
                wait_ms=0.0,
            )

        hits_remaining = max(rate.limit - counter, 0)
        wait_ms = max(window_end_ms - now_ms, 0.0) if counter > rate.limit else 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
        )
```

Use it like any built-in strategy:

```python
from traffik import HTTPThrottle

throttle = HTTPThrottle(
    "api:rolling",
    rate="100/min",
    strategy=RollingWindowStrategy(),
)
```

---

## Best Practices

### 1. Handle `rate.unlimited` First

This is the zero-overhead fast path. Never skip it:

```python
async def __call__(self, key, rate, backend, cost=1):
    if rate.unlimited:
        return 0.0
    # ... your logic
```

### 2. Use `@dataclass(frozen=True)` for Configuration

Frozen dataclasses prevent accidental mutation and make your strategy safe to share across threads and requests:

```python
from dataclasses import dataclass, field
from traffik.types import LockConfig

@dataclass(frozen=True)
class MyStrategy:
    burst_multiplier: float = 1.5
    lock_config: LockConfig = field(default_factory=dict)
```

### 3. Use `backend.lock()` for Multi-Step Operations

Any strategy that reads and then writes needs a lock to prevent race conditions under concurrency. The lock key should be derived from the throttle key:

```python
async with await backend.lock(f"lock:{full_key}:mystrategy", **self.lock_config):
    old_value = await backend.get(some_key)
    new_value = compute(old_value)
    await backend.set(some_key, new_value, expire=ttl)
```

### 4. Use `increment_with_ttl()` When Possible

This is an atomic increment-and-set-TTL operation — much more efficient than `get()` + `increment()` + `expire()` with a lock:

```python
# Good: single atomic operation
counter = await backend.increment_with_ttl(counter_key, amount=cost, ttl=ttl_seconds)

# Less efficient: three operations under a lock
async with await backend.lock(...):
    counter = await backend.increment(counter_key, cost)
    await backend.expire(counter_key, ttl_seconds)
```

### 5. Always Set TTLs

Backend keys that never expire are a memory leak. Set TTLs on everything:

```python
ttl_seconds = max(int(rate.expire // 1000), 1)  # At least 1 second
await backend.set(key, value, expire=ttl_seconds)
```

### 6. Return Milliseconds, Not Seconds

`WaitPeriod` is in **milliseconds**. The rate limit window (`rate.expire`) is also in milliseconds. Don't mix units:

```python
# Correct: milliseconds
wait_ms = window_end_ms - now_ms
return max(wait_ms, 0.0)

# Wrong: accidentally returning seconds
return wait_ms / 1000  # This would be almost always 0
```

### 7. Return 0.0 (not None) to Allow

Returning `0` or `0.0` means "allow". Returning `None` is not valid — always return a float.

---

## Summary Checklist

Before shipping your custom strategy:

- [ ] Handle `rate.unlimited` at the top with `return 0.0`
- [ ] Use `@dataclass(frozen=True)` for the class
- [ ] Include `lock_config: LockConfig` for configurable locking
- [ ] Use `backend.lock()` for any multi-step read/write sequence
- [ ] Prefer `backend.increment_with_ttl()` over separate increment + expire
- [ ] Set TTLs on all backend keys
- [ ] Return milliseconds (not seconds)
- [ ] Implement `get_stat()` for observability
- [ ] Test under concurrency to verify correctness
