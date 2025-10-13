import time

from traffik.backends.base import ThrottleBackend
from traffik.types import Rate, Stringable


__all__ = ["fixed_window_strategy"]


async def fixed_window_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Fixed Window rate limiting strategy.

    The fixed window algorithm works by:
    1. Dividing time into fixed windows (e.g., 1-minute windows)
    2. Counting requests within each window
    3. Resetting the counter when a new window starts

    Pros:
    - Simple and memory-efficient (only 1 counter per key)
    - Easy to understand and implement
    - Constant memory usage regardless of request rate

    Cons:
    - Can allow 2x rate limit at window boundaries
    - Example: With 100 req/min limit, client could make 100 requests at
      00:59 and another 100 at 01:00 (200 requests in 2 seconds)

    Storage format per key:
    - counter: Number of requests in current window
    - window_start: Timestamp when current window started

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    # Get current timestamp in milliseconds
    now = int(time.time() * 1000)

    # Build storage keys
    full_key = await backend.get_key(str(key))
    counter_key = f"{full_key}:counter"
    window_key = f"{full_key}:window"

    # Get current state
    stored_counter = await backend.get(counter_key)
    stored_window = await backend.get(window_key)

    if stored_counter is None or stored_window is None:
        # First request - initialize window
        await backend.set(counter_key, "1", expire=int(rate.expire))
        await backend.set(window_key, str(now), expire=int(rate.expire))
        return 0.0  # Request allowed

    counter = int(stored_counter)
    window_start = int(stored_window)

    # Check if we're still in the same window
    time_in_window = now - window_start

    if time_in_window >= rate.expire:
        # New window - reset counter
        await backend.set(counter_key, "1", expire=int(rate.expire))
        await backend.set(window_key, str(now), expire=int(rate.expire))
        return 0.0  # Request allowed

    # Same window - check if limit exceeded
    if counter >= rate.limit:
        # Calculate wait time until next window
        wait_ms = rate.expire - time_in_window
        return int(wait_ms / 1000) + 1

    # Increment counter
    new_counter = counter + 1
    remaining_time = int(rate.expire - time_in_window)
    await backend.set(counter_key, str(new_counter), expire=remaining_time)

    return 0.0  # Request allowed
