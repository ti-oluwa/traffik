import time

from traffik.backends.base import ThrottleBackend
from traffik.types import Rate, Stringable

__all__ = ["leaky_bucket_strategy", "leaky_bucket_with_queue_strategy"]


async def leaky_bucket_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Leaky Bucket rate limiting strategy.

    The leaky bucket algorithm works by:
    1. Requests are added to a bucket (queue) with a fixed capacity
    2. Requests "leak" from the bucket at a constant rate
    3. If bucket is full, new requests are rejected
    4. Provides smooth, constant rate output

    Unlike token bucket (which allows bursts), leaky bucket enforces
    a strict constant rate by queuing requests and processing them
    at a fixed interval.

    Pros:
    - Enforces perfectly smooth rate (no bursts)
    - Protects downstream services from traffic spikes
    - Simple conceptual model (water leaking from bucket)

    Cons:
    - No burst allowance (can feel restrictive)
    - Requires tracking request queue (more memory)
    - May reject valid requests during temporary spikes

    Implementation:
    This implementation uses a lazy leaking approach - instead of actively
    draining the bucket, we calculate how much should have leaked since
    the last request based on time elapsed.

    Storage format per key:
    - __leakybucket__:level: Current bucket fill level (number of requests in queue)
    - __leakybucket__:last_leak: Timestamp of last leak calculation (milliseconds)

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
    level_key = f"{full_key}:__leakybucket__:level"
    last_leak_key = f"{full_key}:__leakybucket__:last_leak"

    # Get current state
    stored_level = await backend.get(level_key)
    last_leak = await backend.get(last_leak_key)

    # Calculate leak rate: requests per millisecond
    leak_rate = rate.limit / rate.expire

    if stored_level is None or last_leak is None:
        # First request - initialize bucket with one request
        await backend.set(level_key, "1", expire=int(rate.expire * 2))
        await backend.set(last_leak_key, str(now), expire=int(rate.expire * 2))
        return 0.0  # Request allowed

    level = float(stored_level)
    last_leak_time = int(last_leak)

    # Calculate how much has leaked since last check
    time_passed = now - last_leak_time
    leaked_amount = time_passed * leak_rate

    # Update bucket level (can't go below 0)
    level = max(0.0, level - leaked_amount)

    # Check if bucket is full (at capacity)
    if level >= rate.limit:
        # Calculate wait time until there's room in bucket
        # We need at least 1 request worth of space
        wait_needed = (level - rate.limit + 1) / leak_rate
        wait_seconds = int(wait_needed / 1000) + 1
        return wait_seconds

    # Add current request to bucket
    level += 1.0

    # Update state
    expire_ms = int(rate.expire * 2)
    await backend.set(level_key, str(level), expire=expire_ms)
    await backend.set(last_leak_key, str(now), expire=expire_ms)

    return 0.0  # Request allowed


async def leaky_bucket_with_queue_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend
) -> float:
    """
    Leaky Bucket with Queue strategy (strict ordering).

    A variant of leaky bucket that maintains strict FIFO ordering by
    storing actual request timestamps in a queue. This ensures requests
    are processed in the exact order they arrived.

    The bucket "leaks" at a constant rate, and new requests are only
    accepted if there's room in the queue.

    Pros:
    - Strict FIFO ordering guarantees fairness
    - Very predictable behavior
    - Good for scenarios requiring ordered processing

    Cons:
    - Higher memory usage (stores queue of timestamps)
    - More complex implementation
    - Less forgiving than token bucket

    Storage format per key:
    - __leakybucketqueue__:queue: Comma-separated list of request timestamps that haven't "leaked"
    - __leakybucketqueue__:last_leak: Timestamp of last leak calculation

    :param key: The throttling key.
    :param rate: The rate limit definition.
    :param backend: The throttle backend instance.
    :return: The wait period in seconds if throttled, 0 otherwise.
    """
    if rate.unlimited:
        return 0.0

    now = int(time.time() * 1000)

    full_key = await backend.get_key(str(key))
    queue_key = f"{full_key}:__leakybucketqueue__:queue"
    last_leak_key = f"{full_key}:__leakybucketqueue__:last_leak"

    # Get current state
    queue_data = await backend.get(queue_key)
    last_leak = await backend.get(last_leak_key)

    # Calculate leak rate: requests per millisecond
    leak_rate = rate.limit / rate.expire

    if queue_data is None or last_leak is None:
        # First request - initialize queue
        await backend.set(queue_key, str(now), expire=int(rate.expire * 2))
        await backend.set(last_leak_key, str(now), expire=int(rate.expire * 2))
        return 0.0  # Request allowed

    # Parse queue
    queue = [int(ts) for ts in queue_data.split(",") if ts]
    last_leak_time = int(last_leak)

    # Calculate how many requests should have leaked
    time_passed = now - last_leak_time
    requests_to_leak = int(time_passed * leak_rate)

    # Remove leaked requests from queue (FIFO)
    if requests_to_leak > 0:
        queue = queue[requests_to_leak:]
        last_leak_time = now

    # Check if queue is full (at capacity)
    if len(queue) >= rate.limit:
        # Calculate wait time based on oldest request in queue
        oldest_request = queue[0]
        time_since_oldest = now - oldest_request
        time_until_leak = rate.expire - time_since_oldest
        return max(1, int(time_until_leak / 1000) + 1)

    # Add current request to queue
    queue.append(now)

    # Store updated state
    queue_data = ",".join(str(ts) for ts in queue)
    expire_ms = int(rate.expire * 2)
    await backend.set(queue_key, queue_data, expire=expire_ms)
    await backend.set(last_leak_key, str(last_leak_time), expire=expire_ms)

    return 0.0  # Request allowed
