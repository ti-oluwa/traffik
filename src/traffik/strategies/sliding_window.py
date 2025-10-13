import time

from traffik.backends.base import ThrottleBackend
from traffik.types import Rate, Stringable

__all__ = ["sliding_window_strategy", "sliding_window_counter_strategy"]


async def sliding_window_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Sliding Window rate limiting strategy (also known as Sliding Log).

    The sliding window algorithm works by:
    1. Maintaining a log of request timestamps
    2. For each request, removing timestamps older than the window
    3. Counting remaining timestamps to enforce the limit
    4. Window slides continuously with each request (no fixed boundaries)

    Pros:
    - Very accurate - always enforces exact rate in any time window
    - No double-spending at boundaries (unlike fixed window)
    - Smooth rate limiting without sudden resets

    Cons:
    - Higher memory usage (stores timestamp for each request)
    - More CPU intensive (must parse and filter timestamps)
    - Memory grows with request rate (O(limit) per key)

    Example:
        With limit=10 requests per minute:
        - At any moment, only 10 requests allowed in the past 60 seconds
        - If 10 requests at 00:00, next allowed at 00:06 (after oldest expires)
        - Provides true rate limiting without boundary exploitation

    Storage format per key:
    - log: Comma-separated list of request timestamps (milliseconds)

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    now = int(time.time() * 1000)
    window_start = now - int(rate.expire)

    full_key = await backend.get_key(str(key))
    log_key = f"{full_key}:log"

    # Get request log
    log_data = await backend.get(log_key)
    if log_data is None:
        request_log = []
    else:
        # Parse log: comma-separated timestamps
        request_log = [int(ts) for ts in log_data.split(",") if ts]

    # Remove expired entries (outside the sliding window)
    request_log = [ts for ts in request_log if ts > window_start]

    # Check if limit exceeded
    if len(request_log) >= rate.limit:
        # Calculate wait time based on oldest request in window
        oldest_request = min(request_log)
        wait_ms = int(rate.expire) - (now - oldest_request)
        return max(1, int(wait_ms / 1000) + 1)

    # Add current request
    request_log.append(now)

    # Store updated log
    log_data = ",".join(str(ts) for ts in request_log)
    expire_seconds = int(rate.expire * 2) // 1000  # Convert to seconds, 2x for safety
    await backend.set(log_key, log_data, expire=expire_seconds)

    return 0.0  # Request allowed


async def sliding_window_counter_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Sliding Window Counter strategy (weighted approach).

    A memory-efficient alternative to sliding window log that uses counters
    instead of individual timestamps. It works by:
    1. Maintaining counters for current and previous fixed windows
    2. Calculating a weighted count based on overlap with sliding window
    3. Less accurate but much more memory efficient than sliding log

    Formula:
        weighted_count = (prev_window_count * overlap_percentage) + current_window_count

    Pros:
    - Memory efficient (only 2 counters instead of N timestamps)
    - Better than fixed window (smoother rate limiting)
    - Good approximation of true sliding window

    Cons:
    - Less accurate than pure sliding window log
    - Assumes even distribution of requests within windows
    - Can still allow slightly over the limit in edge cases

    Example:
        With limit=10 per minute at timestamp 00:30 (halfway through window):
        - Previous window (23:00-00:00): 6 requests
        - Current window (00:00-01:00): 5 requests
        - Weighted count: (6 * 0.5) + 5 = 8 requests
        - 2 more requests allowed

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    now = int(time.time() * 1000)

    # Calculate current window boundaries
    window_size = int(rate.expire)
    current_window_start = (now // window_size) * window_size
    previous_window_start = current_window_start - window_size

    full_key = await backend.get_key(str(key))
    current_counter_key = f"{full_key}:current:{current_window_start}"
    previous_counter_key = f"{full_key}:previous:{previous_window_start}"

    # Get counters
    current_count_str = await backend.get(current_counter_key)
    previous_count_str = await backend.get(previous_counter_key)

    current_count = int(current_count_str) if current_count_str else 0
    previous_count = int(previous_count_str) if previous_count_str else 0

    # Calculate how far into the current window we are (0.0 to 1.0)
    time_in_current_window = now - current_window_start
    window_progress = time_in_current_window / window_size

    # Calculate weighted count
    # As we progress through current window, previous window has less weight
    previous_weight = 1.0 - window_progress
    weighted_count = (previous_count * previous_weight) + current_count

    # Check if limit exceeded
    if weighted_count >= rate.limit:
        # Calculate wait time
        # We need to wait until enough of the previous window has expired
        if previous_count > 0:
            # Calculate when previous window contribution becomes acceptable
            needed_progress = 1.0 - ((rate.limit - current_count) / previous_count)
            needed_progress = max(window_progress, min(needed_progress, 1.0))
            wait_ms = (needed_progress - window_progress) * window_size
        else:
            # Wait until next window
            wait_ms = window_size - time_in_current_window

        return max(1, int(wait_ms / 1000) + 1)

    # Increment current window counter
    new_count = current_count + 1
    expire_seconds = int(window_size * 2) // 1000  # 2x window size for safety
    await backend.set(current_counter_key, str(new_count), expire=expire_seconds)

    return 0.0  # Request allowed
