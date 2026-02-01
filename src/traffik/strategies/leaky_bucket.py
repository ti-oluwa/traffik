"""Leaky Bucket rate limiting strategies."""

from dataclasses import dataclass, field

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import (
    MsgPackDecodeError,
    dump_data,
    get_blocking_setting,
    get_blocking_timeout,
    load_data,
    time,
)

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
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),  # 100 milliseconds
        )
    )
    """Configuration for the lock used during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply leaky bucket rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:leakybucket:state"

        leak_rate = rate.limit / rate.expire
        # TTL should be 2x window duration, with minimum of 1 second
        ttl_seconds = max(int((2 * rate.expire) / 1000), 1)

        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            old_state_json = await backend.get(state_key)
            # If no existing state, initialize with cost as the bucket level
            if old_state_json is None or old_state_json == "":
                new_state = {"level": float(cost), "last_leak": now}
                await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
                return 0.0

            try:
                state = load_data(old_state_json)
                level = float(state["level"])
                last_leak_time = float(state["last_leak"])
            except (MsgPackDecodeError, KeyError, ValueError):
                # If state is corrupted, reinitialize with cost as the bucket level
                new_state = {"level": float(cost), "last_leak": now}
                await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
                return 0.0

            # Calculate how much has leaked since last check
            time_passed = now - last_leak_time
            leaked_amount = time_passed * leak_rate
            level = max(0.0, level - leaked_amount)

            # If adding this request would overflow the bucket, reject it
            if (level + cost) > rate.limit:
                await backend.set(state_key, old_state_json, expire=ttl_seconds)
                wait_ms = (level - rate.limit + cost) / leak_rate
                return wait_ms

            # If bucket has capacity, add request cost to bucket
            level += cost
            new_state = {"level": level, "last_leak": now}
            await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
            return 0.0

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat:
        """
        Get current statistics for the rate limit.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :return: `StrategyStat` with current hits remaining and wait time.
        """
        if rate.unlimited:
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float("inf"),
                wait_time=0.0,
            )

        now = time() * 1000
        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:leakybucket:state"

        leak_rate = rate.limit / rate.expire

        old_state_json = await backend.get(state_key)
        # If no existing state, bucket is empty
        if old_state_json is None or old_state_json == "":
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=rate.limit,
                wait_time=0.0,
            )

        try:
            state = load_data(old_state_json)
            level = float(state["level"])
            last_leak_time = float(state["last_leak"])
        except (MsgPackDecodeError, KeyError, ValueError):
            # If state is corrupted, assume bucket is empty
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=rate.limit,
                wait_time=0.0,
            )

        # Calculate current level after leakage
        time_passed = now - last_leak_time
        leaked_amount = time_passed * leak_rate
        level = max(0.0, level - leaked_amount)

        # Calculate remaining capacity
        limit = rate.limit
        hits_remaining = max(limit - level, 0)

        # If bucket is over capacity, calculate wait time
        if level > limit:
            wait_ms = (level - limit) / leak_rate
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_time=wait_ms,
        )


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
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),  # 100 milliseconds
        )
    )
    """Configuration for the lock used during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply leaky bucket with queue rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:leakybucketqueue:state"

        leak_rate = rate.limit / rate.expire
        # TTL should be 2x window duration, with minimum of 1 second
        ttl_seconds = max(int((2 * rate.expire) / 1000), 1)

        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            old_state_json = await backend.get(state_key)
            # If no existing state, initialize queue with [timestamp, cost] entry
            if old_state_json is None or old_state_json == "":
                new_state = {"queue": [[now, cost]], "last_leak": now}
                await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
                return 0.0

            try:
                state = load_data(old_state_json)
                # Queue contains [timestamp, cost] tuples
                queue = [[float(ts), float(c)] for ts, c in state["queue"]]
                last_leak_time = float(state["last_leak"])
            except (MsgPackDecodeError, KeyError, ValueError, TypeError):
                # If state is corrupted, reinitialize queue with [timestamp, cost] entry
                new_state = {"queue": [[now, cost]], "last_leak": now}
                await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
                return 0.0

            # Calculate how much cost should have leaked based on time elapsed
            time_passed = now - last_leak_time
            cost_to_leak = time_passed * leak_rate

            # Remove entries from queue head until we've leaked enough cost
            leaked_so_far = 0.0
            while queue and leaked_so_far < cost_to_leak:
                entry_cost = queue[0][1]
                if leaked_so_far + entry_cost <= cost_to_leak:
                    # Fully leak this entry
                    queue.pop(0)
                    leaked_so_far += entry_cost
                else:
                    # Partially leak this entry
                    remaining_cost = entry_cost - (cost_to_leak - leaked_so_far)
                    queue[0][1] = remaining_cost
                    leaked_so_far = cost_to_leak
                    break

            # Update last leak time if we leaked anything
            if leaked_so_far > 0:
                last_leak_time = now

            # Calculate current total cost in queue
            current_queue_cost = sum(c for _, c in queue)

            # If adding this request would overflow the capacity, reject it
            if current_queue_cost + cost > rate.limit:
                await backend.set(state_key, old_state_json, expire=ttl_seconds)
                # Calculate wait time based on how much needs to leak
                cost_over = current_queue_cost + cost - rate.limit
                wait_ms = cost_over / leak_rate
                wait_ms = max(wait_ms, 10)  # minimum wait of 10ms
                return wait_ms

            # If queue has capacity, add request as [timestamp, cost] entry
            queue.append([now, float(cost)])
            new_state = {"queue": queue, "last_leak": last_leak_time}
            await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
            return 0.0

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat:
        """
        Get current statistics for the rate limit.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :return: `StrategyStat` with current hits remaining and wait time.
        """
        if rate.unlimited:
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float("inf"),
                wait_time=0.0,
            )

        now = time() * 1000
        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:leakybucketqueue:state"

        leak_rate = rate.limit / rate.expire

        old_state_json = await backend.get(state_key)
        # If no existing state, queue is empty
        if old_state_json is None or old_state_json == "":
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float(rate.limit),
                wait_time=0.0,
            )

        try:
            state = load_data(old_state_json)
            # Queue contains [timestamp, cost] tuples
            queue = [[float(ts), float(c)] for ts, c in state["queue"]]
            last_leak_time = float(state["last_leak"])
        except (MsgPackDecodeError, KeyError, ValueError, TypeError):
            # If state is corrupted, assume queue is empty
            return StrategyStat(
                key=key,
                rate=rate,
                hits_remaining=float(rate.limit),
                wait_time=0.0,
            )

        # Calculate how much cost should have leaked
        time_passed = now - last_leak_time
        cost_to_leak = time_passed * leak_rate

        # Simulate leaking without modifying queue
        leaked_so_far = 0.0
        simulated_queue = [list(entry) for entry in queue]  # Deep copy
        while simulated_queue and leaked_so_far < cost_to_leak:
            entry_cost = simulated_queue[0][1]
            if leaked_so_far + entry_cost <= cost_to_leak:
                # Fully leak this entry
                simulated_queue.pop(0)
                leaked_so_far += entry_cost
            else:
                # Partially leak this entry
                remaining_cost = entry_cost - (cost_to_leak - leaked_so_far)
                simulated_queue[0][1] = remaining_cost
                leaked_so_far = cost_to_leak
                break

        # Calculate current total cost in queue after simulated leak
        current_queue_cost = sum(c for _, c in simulated_queue)

        # Calculate remaining capacity
        limit = rate.limit
        hits_remaining = max(limit - current_queue_cost, 0.0)

        # If over capacity, calculate wait time
        if current_queue_cost > limit:
            cost_over = current_queue_cost - limit
            wait_ms = cost_over / leak_rate
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_time=wait_ms,
        )
