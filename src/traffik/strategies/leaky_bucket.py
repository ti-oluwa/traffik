"""Leaky Bucket rate limiting strategies."""

from dataclasses import dataclass, field

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, Stringable, WaitPeriod
from traffik.utils import JSONDecodeError, dump_json, load_json, time

__all__ = ["LeakyBucketStrategy", "LeakyBucketWithQueueStrategy"]


@dataclass(frozen=True)
class LeakyBucketStrategy:
    """
    Leaky Bucket rate limiting strategy.

    Models rate limiting as a bucket with a hole that leaks at a constant rate.
    Requests fill the bucket, and if the bucket overflows, requests are rejected.
    This enforces perfectly smooth traffic output.

    **How it works:**
    1. Bucket has fixed capacity (rate limit)
    2. Each request adds to bucket level
    3. Bucket "leaks" at constant rate (requests per unit time)
    4. If bucket is full, new requests are rejected
    5. Bucket level decreases over time as it leaks

    **Pros:**
    - Enforces perfectly smooth rate (no bursts allowed)
    - Protects downstream services from traffic spikes
    - Predictable and consistent behavior
    - Good for APIs with strict rate requirements

    **Cons:**
    - No burst allowance (can feel restrictive for users)
    - Legitimate burst traffic may be rejected
    - Less forgiving than token bucket

    **When to use:**
    - APIs calling rate-limited third-party services
    - When you need to guarantee smooth traffic output
    - Protecting downstream services from overload
    - When bursts are not desirable

    **Storage format:**
    - Key: `{namespace}:{key}:leakybucket:state`
    - Value: JSON `{"level": float, "last_leak": timestamp_ms}`
    - TTL: 2x window duration for safety

    **Example:**

    ```python
    from traffik.rates import Rate
    from traffik.strategies import LeakyBucketStrategy

    # Smooth traffic: 100 requests per minute, no bursts
    rate = Rate.parse("100/1m")
    strategy = LeakyBucketStrategy()
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = max(wait_ms / 1000, 1)
        # Bucket is full, must wait
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=1.5,
        )
    )
    """Configuration for the lock used during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> WaitPeriod:
        """
        Apply leaky bucket rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        full_key = await backend.get_key(str(key))
        state_key = f"{full_key}:leakybucket:state"

        leak_rate = rate.limit / rate.expire
        # TTL should be 2x window duration, with minimum of 1 second
        ttl_seconds = max(int((2 * rate.expire) / 1000), 1)

        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            old_state_json = await backend.get(state_key)
            if old_state_json is None or old_state_json == "":
                new_state = {"level": 1.0, "last_leak": now}
                await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
                return 0.0

            try:
                state = load_json(old_state_json)
                level = float(state["level"])
                last_leak_time = float(state["last_leak"])
            except (JSONDecodeError, KeyError, ValueError):
                new_state = {"level": 1.0, "last_leak": now}
                await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
                return 0.0

            time_passed = now - last_leak_time
            leaked_amount = time_passed * leak_rate
            level = max(0.0, level - leaked_amount)

            # Check if adding this request would overflow the bucket
            if (level + 1.0) > rate.limit:
                # Bucket is full, calculate wait time
                await backend.set(state_key, old_state_json, expire=ttl_seconds)
                wait_ms = (level - rate.limit + 1.0) / leak_rate
                return wait_ms

            # Add request to bucket
            level += 1.0
            new_state = {"level": level, "last_leak": now}
            await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
            return 0.0


@dataclass(frozen=True)
class LeakyBucketWithQueueStrategy:
    """
    Leaky Bucket with Queue strategy (strict FIFO ordering).

    Enhanced leaky bucket that maintains strict FIFO ordering by storing
    actual request timestamps in a queue. Guarantees fairness and ordered
    processing.

    **How it works:**
    1. Maintain queue of request timestamps (FIFO)
    2. Queue leaks at constant rate (processes requests in order)
    3. New requests added to end of queue
    4. If queue is full, reject new requests
    5. Oldest requests leak out first

    **Pros:**
    - Strict FIFO ordering (fairness guarantee)
    - Very predictable and deterministic behavior
    - Good for scenarios requiring ordered processing
    - No request can "cut in line"

    **Cons:**
    - Higher memory usage (stores full queue)
    - Memory grows with rate limit
    - More complex than standard leaky bucket
    - Still no burst allowance

    **When to use:**
    - When fairness and ordering are critical
    - Processing queues where order matters
    - APIs where request order affects results
    - When you need deterministic behavior

    **Storage format:**
    - Key: `{namespace}:{key}:leakybucketqueue:state`
    - Value: JSON `{"queue": [ts1, ts2, ...], "last_leak": timestamp_ms}`
    - TTL: 2x window duration for safety

    **Example:**

    ```python
    from traffik.rates import Rate
    from traffik.strategies import LeakyBucketWithQueueStrategy

    # Strict FIFO processing: 100 requests per minute
    rate = Rate.parse("100/1m")
    strategy = LeakyBucketWithQueueStrategy()
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = max(wait_ms / 1000, 1)
        # Queue is full, requests processed in order
        raise HTTPException(429, f"Queue full. Retry in {wait_seconds}s")
    ```
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=1.5,
        )
    )
    """Configuration for the lock used during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> WaitPeriod:
        """
        Apply leaky bucket with queue rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        full_key = await backend.get_key(str(key))
        state_key = f"{full_key}:leakybucketqueue:state"

        leak_rate = rate.limit / rate.expire
        # TTL should be 2x window duration, with minimum of 1 second
        ttl_seconds = max(int((2 * rate.expire) / 1000), 1)

        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            old_state_json = await backend.get(state_key)
            if old_state_json is None or old_state_json == "":
                new_state = {"queue": [now], "last_leak": now}
                await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
                return 0.0

            try:
                state = load_json(old_state_json)
                queue = [float(ts) for ts in state["queue"]]
                last_leak_time = float(state["last_leak"])
            except (JSONDecodeError, KeyError, ValueError, TypeError):
                new_state = {"queue": [now], "last_leak": now}
                await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
                return 0.0

            time_passed = now - last_leak_time
            requests_to_leak = int(time_passed * leak_rate)
            if requests_to_leak > 0:
                queue = queue[requests_to_leak:]
                last_leak_time = now

            # Check if adding this request would overflow the queue
            if (len(queue) + 1) > rate.limit:
                # Queue is full
                await backend.set(state_key, old_state_json, expire=ttl_seconds)
                oldest_request = queue[0]
                time_since_oldest = now - oldest_request
                time_until_leak = rate.expire - time_since_oldest
                wait_ms = max(time_until_leak, 10)  # minimum wait of 10ms
                return wait_ms

            # Add request to queue
            queue.append(now)
            new_state = {"queue": queue, "last_leak": last_leak_time}
            await backend.set(state_key, dump_json(new_state), expire=ttl_seconds)
            return 0.0
