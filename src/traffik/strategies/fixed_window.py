"""Fixed Window rate limiting strategy implementation."""

from dataclasses import dataclass

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod
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
    - Key: `{namespace}:{key}:fixedwindow:{window_start_timestamp}`
    - Value: Request counter (integer)
    - TTL: Window duration + 1 second buffer

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

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> WaitPeriod:
        """
        Apply fixed window rate limiting strategy.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire
        current_window_start = (now // window_duration_ms) * window_duration_ms

        full_key = await backend.get_key(str(key))
        window_key = f"{full_key}:fixedwindow:{current_window_start}"
        # Add plus one buffer second, just to insure TTL covers the window
        # Especially important for very short windows
        ttl_seconds = (window_duration_ms // 1000) + 1

        async with await backend.lock(
            f"lock:{window_key}", blocking=True, blocking_timeout=1
        ):
            counter = await backend.increment_with_ttl(
                window_key, amount=1, ttl=int(ttl_seconds)
            )

        if counter > rate.limit:
            time_in_window = now - current_window_start
            wait_ms = window_duration_ms - time_in_window
            return wait_ms
        return 0.0
