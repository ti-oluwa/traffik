# Configuration

Traffik's locking behavior can be tuned globally — either through environment variables (great for containers) or programmatically at startup.

Most of these settings are about lock behavior, because locks are where the tradeoffs between accuracy, latency, and throughput live. The defaults are sensible for most applications, but understanding them lets you squeeze more performance out of Traffik when you need it.

---

## Global Lock Settings

### Programmatic Configuration

```python
from traffik.config import (
    set_lock_ttl,
    set_lock_blocking,
    set_lock_blocking_timeout,
    get_lock_ttl,
    get_lock_blocking,
    get_lock_blocking_timeout,
)

# Set before creating backends/throttles — ideally at app startup
set_lock_ttl(30.0)                # Lock expires after 30 seconds
set_lock_blocking(True)           # Locks block when contended
set_lock_blocking_timeout(5.0)    # Give up after 5 seconds of waiting

# Read current settings
ttl = get_lock_ttl()              # -> 30.0
blocking = get_lock_blocking()    # -> True
timeout = get_lock_blocking_timeout()  # -> 5.0
```

### Environment Variables

Configure via environment variables for containerized deployments:

```bash
# Lock TTL in seconds (float). None = no automatic expiry.
export TRAFFIK_DEFAULT_LOCK_TTL=30.0

# Whether to block when lock is contended. Default: true
export TRAFFIK_DEFAULT_BLOCKING=true

# Blocking timeout in seconds. None = wait indefinitely.
export TRAFFIK_DEFAULT_BLOCKING_TIMEOUT=5.0
```

| Variable | Type | Default | Description |
|---|---|---|---|
| `TRAFFIK_DEFAULT_LOCK_TTL` | float | `None` | Lock auto-expiry in seconds |
| `TRAFFIK_DEFAULT_BLOCKING` | bool | `true` | Block when lock is contended |
| `TRAFFIK_DEFAULT_BLOCKING_TIMEOUT` | float | `None` | Max seconds to wait for lock |

---

## Lock Blocking Settings Reference

| Profile | `blocking` | `blocking_timeout` | `lock_ttl` | Use Case |
|---|---|---|---|---|
| **High-accuracy** | `True` | `None` | `30.0` | Strict rate limiting; clients wait for accurate counts |
| **Low-latency** | `True` | `1.0` | `10.0` | Prefer fast responses; accept occasional imprecision |
| **High-concurrency** | `False` | `None` | `5.0` | Non-blocking; fail fast under contention |
| **Dev / Testing** | `True` | `10.0` | `60.0` | Generous timeouts; accuracy matters less than debugging |

### High-Accuracy Configuration

Use this when rate limit correctness is critical (billing, security, compliance):

```python
set_lock_blocking(True)
set_lock_blocking_timeout(None)   # Wait as long as necessary
set_lock_ttl(30.0)                # Safety TTL to prevent deadlock
```

Tradeoff: requests will queue under lock contention, increasing latency. If your backend is slow, this can cause cascading delays.

### Low-Latency Configuration

Use this for user-facing APIs where a few extra requests sneaking through is acceptable:

```python
set_lock_blocking(True)
set_lock_blocking_timeout(1.0)   # Give up after 1 second
set_lock_ttl(10.0)
```

Tradeoff: if the lock can't be acquired within 1 second, the strategy runs without it — potentially allowing a small burst above the limit.

### High-Concurrency Configuration

Use this for very high request volumes where you need maximum throughput and can tolerate some over-counting:

```python
set_lock_blocking(False)         # Don't wait at all — fail immediately
set_lock_ttl(5.0)
```

Tradeoff: significant accuracy loss under high concurrency. This is essentially optimistic locking. Consider whether `FixedWindow` without locking (windows >= 1s) is what you actually want.

---

## Strategy-Level Lock Overrides

Each strategy instance can have its own lock configuration that overrides the global settings:

```python
from traffik.strategies import FixedWindow, TokenBucket
from traffik.types import LockConfig

# High-accuracy lock for a sensitive throttle
sensitive_strategy = TokenBucket(
    lock_config=LockConfig(
        ttl=30.0,
        blocking=True,
        blocking_timeout=5.0,
    )
)

# Fast-fail lock for a high-throughput throttle
fast_strategy = FixedWindow(
    lock_config=LockConfig(
        ttl=5.0,
        blocking=False,
    )
)

from traffik import HTTPThrottle

billing_throttle = HTTPThrottle("billing", rate="10/min", strategy=sensitive_strategy)
feed_throttle = HTTPThrottle("feed", rate="1000/min", strategy=fast_strategy)
```

---

## Sub-Second Windows and Locking

!!! warning "Sub-second windows always use locking"
    For `FixedWindow` and `SlidingWindowCounter`, windows **smaller than 1 second** always acquire a distributed lock, regardless of global settings. This is necessary because atomic `increment_with_ttl` alone isn't sufficient for sub-millisecond precision — the window boundary tracking requires a separate read/write that must be protected.

    For windows **of 1 second or longer**, `FixedWindow` uses only `increment_with_ttl` (which is atomic) and skips the lock entirely. This is the fastest path.

```python
# No lock needed — uses atomic increment_with_ttl
throttle = HTTPThrottle("api", rate="100/min")   # 60 seconds >= 1 second

# Always uses locking — requires window boundary management
throttle = HTTPThrottle("api", rate="10/500ms")  # 500ms < 1 second
```

!!! tip "Prefer >= 1s windows in production"
    Sub-second windows are great for bursty traffic control, but they add locking overhead. If you don't specifically need millisecond-level precision, use windows of 1 second or more and enjoy the lock-free fast path.

---

## Lock Contention and Its Effects

When many requests compete for the same lock, you'll see:

| Effect | Symptom | Mitigation |
|---|---|---|
| Increased latency | p95/p99 response times spike | Reduce `blocking_timeout`; use non-locking strategies |
| Lock timeouts | `LockTimeout` errors in logs | Increase `lock_ttl`; check backend latency |
| Throughput degradation | Requests per second drops | Switch to `FixedWindow` with >= 1s windows; use lock striping |

### Mitigation Strategies

1. **Use longer windows**: The easiest fix. `rate="100/min"` has no lock overhead; `rate="100/500ms"` does.

2. **Tune `blocking_timeout`**: Set a low timeout (e.g., 0.5s) to fail fast rather than queue up. Accept minor inaccuracy as the tradeoff.

3. **Non-locking strategies for >= 1s windows**: `FixedWindow` with >= 1s windows is lock-free by design. It's the fastest built-in strategy.

4. **Lock striping for InMemory**: If you're on `InMemoryBackend`, configure more shards to reduce contention:

    ```python
    from traffik.backends.inmemory import InMemoryBackend

    backend = InMemoryBackend(
        namespace="myapp",
        number_of_shards=64,   # More shards = less lock contention
    )
    ```

5. **Connection pooling for Redis/Memcached**: Lock acquisition latency is dominated by round-trip time to the backend. Increase pool size to reduce queuing.
