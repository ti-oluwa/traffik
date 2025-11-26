"""Sliding Window Rate Limiting Strategies"""

import typing
from dataclasses import dataclass, field

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import JSONDecodeError, dump_json, load_json, time

__all__ = ["SlidingWindowLogStrategy", "SlidingWindowCounterStrategy"]


@dataclass(frozen=True)
class SlidingWindowLogStrategy:
    """
    Sliding Window Log rate limiting (most accurate).

    Maintains a log of request timestamps and evaluates rate limit over
    a continuously sliding time window. This is the most accurate rate
    limiting algorithm but uses more memory.

    **How it works:**
    1. Store timestamp of each request in a log
    2. On new request, remove timestamps older than window duration
    3. Count remaining timestamps and compare to limit
    4. Window slides continuously with each request (no fixed boundaries)

    **Pros:**
    - Most accurate rate limiting (true sliding window)
    - No burst traffic at boundaries
    - Always enforces exact rate in any time window
    - Smooth and predictable behavior

    **Cons:**
    - Memory intensive (stores one timestamp per request)
    - Memory grows with rate limit (O(limit) space per key)
    - Slightly slower than counter-based strategies

    **When to use:**
    - When accuracy is critical
    - When you need to prevent boundary exploitation
    - Financial APIs, payment processing, security-critical endpoints
    - When memory usage is not a concern

    **Storage format:**
    - Key: `{namespace}:{key}:slidinglog`
    - Value: JSON array of request timestamps in milliseconds
    - TTL: Window duration + 1 second buffer

    **Example:**
    ```python
    from traffik.rates import Rate
    from traffik.strategies import SlidingWindowLogStrategy

    # Strict limit: exactly 100 requests per minute, no bursts
    rate = Rate.parse("100/1m")
    strategy = SlidingWindowLogStrategy()
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = int(wait_ms / 1000)
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```

    :param key: The throttling key (e.g., user ID, IP address).
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: Wait time in milliseconds if throttled, 0.0 if allowed.
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=1.5,
        )
    )
    """Configuration for backend locking during log updates."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply sliding window log rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire
        window_start = now - window_duration_ms

        full_key = await backend.get_key(str(key))
        log_key = f"{full_key}:slidinglog"
        ttl_seconds = max(int(window_duration_ms // 1000), 1)  # At least 1s

        async with await backend.lock(f"lock:{log_key}", **self.lock_config):
            old_log_json = await backend.get(log_key)
            # If log exists, load and parse entries as [timestamp, cost] tuples
            if old_log_json and old_log_json != "":
                try:
                    entries: typing.List[typing.List[float]] = load_json(old_log_json)
                except JSONDecodeError:
                    entries = []
            else:
                entries = []

            # Filter entries to only include those within the current window and sum their costs
            valid_entries = [
                [float(ts), float(c)] for ts, c in entries if ts > window_start
            ]
            current_cost_sum = sum(c for _, c in valid_entries)

            # If adding this request's cost would exceed limit, reject it
            if current_cost_sum + cost > rate.limit:
                # Find the oldest entry to calculate wait time
                oldest_timestamp = min(ts for ts, _ in valid_entries)
                wait_ms = (oldest_timestamp + window_duration_ms) - now
                await backend.set(log_key, dump_json(valid_entries), expire=ttl_seconds)
                return wait_ms

            # If within limit, add this request as [timestamp, cost] entry
            valid_entries.append([now, float(cost)])
            await backend.set(log_key, dump_json(valid_entries), expire=ttl_seconds)
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
        window_duration_ms = rate.expire
        window_start = now - window_duration_ms

        full_key = await backend.get_key(str(key))
        log_key = f"{full_key}:slidinglog"

        old_log_json = await backend.get(log_key)
        # If log exists, load and parse entries as [timestamp, cost] tuples
        if old_log_json and old_log_json != "":
            try:
                entries: typing.List[typing.List[float]] = load_json(old_log_json)
            except JSONDecodeError:
                entries = []
        else:
            entries = []

        # Filter entries to only include those within the current window and sum their costs
        valid_entries = [
            [float(ts), float(c)] for ts, c in entries if ts > window_start
        ]
        current_cost_sum = sum(c for _, c in valid_entries)

        # Calculate remaining capacity
        hits_remaining = max(rate.limit - current_cost_sum, 0.0)

        # If over limit, calculate wait time
        if current_cost_sum > rate.limit:
            oldest_timestamp = min(ts for ts, _ in valid_entries)
            wait_ms = (oldest_timestamp + window_duration_ms) - now
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_time=wait_ms,
        )


@dataclass(frozen=True)
class SlidingWindowCounterStrategy:
    """
    Sliding Window Counter rate limiting (hybrid approach).

    Combines fixed window counters with weighted calculation to approximate
    a sliding window. Offers good accuracy with low memory usage.

    **How it works:**
    1. Maintain counters for current and previous fixed windows
    2. Calculate weighted count based on position in current window
    3. Formula: `weighted_count = (prev_count * overlap%) + curr_count`
    4. Overlap% decreases as we move through current window

    **Pros:**
    - Better accuracy than fixed window
    - Memory efficient (only 2 counters per key)
    - Good balance between performance and accuracy
    - Significantly reduces boundary burst issues

    **Cons:**
    - Not as accurate as sliding window log
    - Assumes even request distribution within windows
    - Still allows small bursts in edge cases

    **When to use:**
    - General purpose rate limiting
    - When you need better accuracy than fixed window
    - High-traffic APIs where memory is a concern
    - Good default choice for most applications

    **Storage format:**
    - Key: `{namespace}:{key}:slidingcounter:{window_id}`
    - Value: Request counter (integer)
    - Uses two consecutive windows for weighted calculation
    - TTL: Window duration + 1 second buffer

    **Algorithm example:**
        At timestamp 00:30 (halfway through 1-minute window):
        - Previous window (23:00-00:00): 60 requests
        - Current window (00:00-01:00): 50 requests
        - Overlap: 30s out of 60s = 50%
        - Weighted count: (60 * 0.5) + 50 = 80 requests
        - If limit is 100, 20 more requests allowed

    **Example:**
    ```python
    from traffik.rates import Rate
    from traffik.strategies import SlidingWindowCounterStrategy

    # Balanced approach: good accuracy with low memory
    rate = Rate.parse("100/1m")
    strategy = SlidingWindowCounterStrategy()
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = int(wait_ms / 1000)
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=1.5,
        )
    )
    """Configuration for backend locking during counter updates."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply sliding window counter rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire

        current_window_id = int(now // window_duration_ms)
        previous_window_id = current_window_id - 1

        time_in_current_window = now % window_duration_ms
        overlap_percentage = 1.0 - (time_in_current_window / window_duration_ms)

        full_key = await backend.get_key(str(key))
        current_window_key = f"{full_key}:slidingcounter:{current_window_id}"
        previous_window_key = f"{full_key}:slidingcounter:{previous_window_id}"

        # TTL must be 2x window duration so previous window is available
        # throughout the entire current window. Minimum 1 second for cleanup.
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        async with await backend.lock(
            f"lock:{previous_window_key}", **self.lock_config
        ):
            # Increment current window counter by cost
            current_count = await backend.increment_with_ttl(
                current_window_key, amount=cost, ttl=ttl_seconds
            )

            # Get previous window counter
            previous_count_str = await backend.get(previous_window_key)
            if previous_count_str and previous_count_str != "":
                try:
                    previous_count = int(previous_count_str)
                    # Refresh TTL to keep previous window alive
                    await backend.set(
                        previous_window_key, previous_count_str, expire=ttl_seconds
                    )
                except (ValueError, TypeError):
                    previous_count = 0
            else:
                previous_count = 0

            # Calculate weighted count using sliding window algorithm
            weighted_count = (previous_count * overlap_percentage) + current_count

            # If weighted count exceeds limit, reject request
            if weighted_count > rate.limit:
                requests_over = weighted_count - rate.limit
                if previous_count > 0:
                    wait_ratio = requests_over / previous_count
                    wait_ms = wait_ratio * time_in_current_window
                else:
                    wait_ms = window_duration_ms - time_in_current_window
                return wait_ms
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
        window_duration_ms = rate.expire

        current_window_id = int(now // window_duration_ms)
        previous_window_id = current_window_id - 1

        time_in_current_window = now % window_duration_ms
        overlap_percentage = 1.0 - (time_in_current_window / window_duration_ms)

        full_key = await backend.get_key(str(key))
        current_window_key = f"{full_key}:slidingcounter:{current_window_id}"
        previous_window_key = f"{full_key}:slidingcounter:{previous_window_id}"

        # Get current window counter
        current_count_str = await backend.get(current_window_key)
        current_count = int(current_count_str) if current_count_str else 0

        # Get previous window counter
        previous_count_str = await backend.get(previous_window_key)
        previous_count = int(previous_count_str) if previous_count_str else 0

        # Calculate weighted count using sliding window algorithm
        weighted_count = (previous_count * overlap_percentage) + current_count

        # Calculate remaining hits
        hits_remaining = max(rate.limit - weighted_count, 0.0)

        # If over limit, calculate wait time
        if weighted_count > rate.limit:
            requests_over = weighted_count - rate.limit
            if previous_count > 0:
                wait_ratio = requests_over / previous_count
                wait_ms = wait_ratio * time_in_current_window
            else:
                wait_ms = window_duration_ms - time_in_current_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_time=wait_ms,
        )
