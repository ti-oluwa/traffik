"""Fixed Window rate limiting strategy implementation."""

from dataclasses import dataclass, field

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import time

__all__ = ["FixedWindowStrategy"]


@dataclass(frozen=True)
class FixedWindowStrategy:
    """
    Fixed Window rate limiting strategy.

    Divides time into fixed windows (e.g., 1-minute intervals) and counts
    requests within each window. When a window expires, the counter resets.

    **How it works:**
    1. Time is divided into fixed windows aligned to boundaries
    2. Each request increments a counter for the current window
    3. If counter exceeds limit, requests are throttled
    4. Counter automatically resets when new window starts

    **Pros:**
    - Simple and easy to understand
    - Memory efficient (only 1 counter per window)
    - Constant memory usage regardless of request rate
    - Fast performance with atomic operations

    **Cons:**
    - Burst traffic at window boundaries (can allow up to 2x limit)
    - Example: With 100 req/min, client could make 100 requests at 00:59
      and another 100 at 01:00 (200 requests in 2 seconds)

    **When to use:**
    - Simple rate limiting needs
    - When burst traffic at boundaries is acceptable
    - When memory efficiency is important
    - High-throughput APIs where slight boundary issues are tolerable

    **Storage format:**
    - Window start key: `{namespace}:{key}:fixedwindow:start` - Stores current window start timestamp
    - Counter key: `{namespace}:{key}:fixedwindow:counter` - Request counter (integer)
    - TTL: Window duration + 2 seconds buffer (minimum 2 seconds for cleanup)

    **Example:**

    ```python
    from traffik.rates import Rate
    from traffik.strategies import FixedWindowStrategy

    # Allow 100 requests per minute
    rate = Rate.parse("100/m")
    strategy = FixedWindowStrategy()
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = max(wait_ms / 1000, 1)
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=0.1, # 100 milliseconds
        )
    )
    """Configuration for backend locking during rate limit checks."""

    # The `backend.increment_with_ttl` method should have been used here but since using it
    # means a new window start is based on the expiry of the counter key, and the minimum
    # allowable TTL for the counter key is 1 second, it could lead to inaccuracies for very small windows.
    # Especially for windows smaller than 1 second. So we either clamp the wait time to
    # 1 second minimum, which is not ideal, assuming the throttling supports sub-second wait times,
    # or we manually manage the window start time separately. Which is what we do here.

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply fixed window rate limiting strategy.

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
        current_window_start = (now // window_duration_ms) * window_duration_ms

        full_key = await backend.get_key(str(key))
        base_key = f"{full_key}:fixedwindow"
        window_start_key = f"{base_key}:start"
        counter_key = f"{base_key}:counter"

        # TTL should be at least 1 second for cleanup, but we track window time separately
        # Add buffer to ensure keys don't expire during a valid window
        ttl_seconds = max(int(window_duration_ms // 1000), 2)
        async with await backend.lock(f"lock:{base_key}", **self.lock_config):
            # Get the stored window start time
            stored_window_start = await backend.get(window_start_key)

            # Check if we're in a new window
            if (
                stored_window_start is None
                or int(stored_window_start) != current_window_start
            ):
                # If we are in a new window, reset counter and store new window start
                await backend.set(
                    window_start_key, str(int(current_window_start)), expire=ttl_seconds
                )
                await backend.set(counter_key, str(cost), expire=ttl_seconds)
                counter = cost
            else:
                # If we are in the existing/same window, increment the counter by the cost
                counter = await backend.increment_with_ttl(
                    counter_key, amount=cost, ttl=ttl_seconds
                )

        if counter > rate.limit:
            time_in_window = now - current_window_start
            wait_ms = window_duration_ms - time_in_window
            return max(wait_ms, 0.0)
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
        current_window_start = (now // window_duration_ms) * window_duration_ms

        full_key = await backend.get_key(str(key))
        base_key = f"{full_key}:fixedwindow"
        window_start_key = f"{base_key}:start"
        counter_key = f"{base_key}:counter"

        # Get the stored window start time
        stored_window_start = await backend.get(window_start_key)

        # Check if we're in a new window or no data exists
        if (
            stored_window_start is None
            or int(stored_window_start) != current_window_start
        ):
            # If we are in a new window or no data set counter as 0
            counter = 0
        else:
            # If we are in an existing/valid window, get current counter value
            counter_value = await backend.get(counter_key)
            counter = int(counter_value) if counter_value else 0

        hits_remaining = max(rate.limit - counter, 0)
        if counter > rate.limit:
            time_in_window = now - current_window_start
            wait_ms = window_duration_ms - time_in_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_time=wait_ms,
        )
