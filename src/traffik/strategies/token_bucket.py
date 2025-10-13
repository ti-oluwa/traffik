import asyncio

from traffik.backends.base import ThrottleBackend
from traffik.types import Rate, Stringable


__all__ = ["token_bucket_strategy", "sliding_window_token_bucket_strategy"]


async def token_bucket_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Efficient Token Bucket rate limiting strategy.

    The token bucket algorithm works by:
    1. Starting with a full bucket of tokens (equal to the rate limit)
    2. Removing one token per request
    3. Refilling tokens at a constant rate over time
    4. Allowing bursts up to the bucket capacity

    This implementation uses lazy token refill - tokens are only calculated
    when needed, avoiding unnecessary background processing.

    Storage format per key:
    - tokens: Current number of tokens (float for precision)
    - last_refill: Timestamp of last token calculation (milliseconds)

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    # Get current timestamp in milliseconds
    now = int(asyncio.get_event_loop().time() * 1000)

    # Build storage keys
    full_key = await backend.get_key(str(key))
    tokens_key = f"{full_key}:__token_bucket__:tokens"
    last_refill_key = f"{full_key}:__token_bucket__:last_refill"

    # Get current state
    stored_tokens = await backend.get(tokens_key)
    last_refill = await backend.get(last_refill_key)

    if stored_tokens is None or last_refill is None:
        # First request - initialize bucket
        tokens = float(rate.limit - 1)  # Consume one token for this request

        # Store state with expiration (2x the rate period for safety)
        expire_ms = int(rate.expire * 2)
        await backend.set(tokens_key, str(tokens), expire=expire_ms)
        await backend.set(last_refill_key, str(now), expire=expire_ms)

        return 0.0  # Request allowed

    # Calculate token refill
    tokens = float(stored_tokens)
    last_refill_time = int(last_refill)
    time_passed = now - last_refill_time

    # Calculate refill rate: tokens per millisecond
    refill_rate = rate.limit / rate.expire

    # Calculate tokens to add (lazy refill)
    tokens_to_add = time_passed * refill_rate
    tokens = min(tokens + tokens_to_add, float(rate.limit))  # Cap at bucket capacity

    # Try to consume one token
    if tokens >= 1.0:
        # Request allowed
        tokens -= 1.0

        # Update state
        expire_ms = int(rate.expire * 2)
        await backend.set(tokens_key, str(tokens), expire=expire_ms)
        await backend.set(last_refill_key, str(now), expire=expire_ms)

        return 0.0

    # Not enough tokens - calculate wait time
    tokens_needed = 1.0 - tokens
    wait_ms = tokens_needed / refill_rate

    # Return wait time in seconds (rounded up)
    return int(wait_ms / 1000) + 1


async def sliding_window_token_bucket_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Hybrid sliding window + token bucket strategy.

    Combines the smoothness of token bucket with the precision of sliding window.
    This prevents the "double spending" problem where a client could make 2x
    requests around a time boundary.

    Uses a sliding log approach with token bucket refill mechanics:
    - Maintains timestamps of recent requests
    - Uses token bucket for rate calculation
    - Slides the window continuously (no fixed boundaries)

    More memory intensive but more accurate for strict rate limiting.

    Example:
        With limit=10 requests per minute:
        - Fixed window: Could allow 20 requests (10 at end of minute 1, 10 at start of minute 2)
        - Sliding window: Always enforces exactly 10 requests in any 60-second period

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    now = int(asyncio.get_event_loop().time() * 1000)
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
    expire_ms = int(rate.expire * 2) // 1000  # Convert to seconds for backend
    await backend.set(log_key, log_data, expire=expire_ms)

    return 0.0
