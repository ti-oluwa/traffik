"""Token Bucket rate limiting strategies."""

import typing
from dataclasses import dataclass, field

from typing_extensions import TypedDict

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import MsgPackDecodeError, dump_data, load_data, time

__all__ = [
    "TokenBucket",
    "TokenBucketWithDebt",
    "TokenBucketStrategy",
    "TokenBucketWithDebtStrategy",
    "TokenBucketStatMetadata",
    "TokenBucketWithDebtStatMetadata",
]


class TokenBucketStatMetadata(TypedDict):
    """
    Metadata for `TokenBucketStrategy` statistics.

    The token bucket strategy models rate limiting as a bucket that holds
    tokens, which refill at a constant rate.
    """

    strategy: typing.Literal["token_bucket"]
    """Strategy identifier, always "token_bucket"."""

    tokens: float
    """Current number of tokens in the bucket."""

    capacity: int
    """Maximum capacity of the bucket (burst size)."""

    refill_rate_per_ms: float
    """Rate at which tokens are added to the bucket (tokens per millisecond)."""

    last_refill_ms: float
    """Timestamp of the last token refill calculation in milliseconds since epoch."""


class TokenBucketWithDebtStatMetadata(TypedDict):
    """
    Metadata for `TokenBucketWithDebtStrategy` statistics.

    Enhanced token bucket that allows going into "debt" (negative token balance)
    for smoother handling of traffic spikes.
    """

    strategy: typing.Literal["token_bucket_with_debt"]
    """Strategy identifier, always "token_bucket_with_debt"."""

    tokens: float
    """Current token balance (can be negative when in debt)."""

    capacity: int
    """Maximum positive token capacity (burst size)."""

    max_debt: int
    """Maximum allowed negative token balance (overdraft limit)."""

    current_debt: float
    """Current debt amount (0.0 if tokens >= 0, otherwise abs(tokens))."""

    refill_rate_per_ms: float
    """Rate at which tokens are added to the bucket (tokens per millisecond)."""

    last_refill_ms: float
    """Timestamp of the last token refill calculation in milliseconds since epoch."""


@dataclass(frozen=True)
class TokenBucketStrategy:
    """
    Token Bucket rate limiting strategy (smooth traffic with burst support).

    Models rate limiting as a bucket that holds tokens. Tokens refill at a
    constant rate, and each request consumes one token. Allows controlled
    bursts while maintaining average rate over time.

    **How it works:**
    1. Bucket starts full with `capacity` tokens (default = rate limit)
    2. Tokens refill continuously at configured rate (e.g., 100/minute)
    3. Each request consumes 1 token from the bucket
    4. If bucket has < 1 token, requests must wait until token available
    5. Bucket capacity limits maximum burst size

    **Pros:**
    - Allows traffic bursts up to bucket capacity
    - Smooth traffic distribution over time
    - Self-recovering (tokens automatically refill)
    - Good for APIs with legitimate occasional spikes
    - More forgiving than leaky bucket

    **Cons:**
    - Slightly more complex than fixed window
    - Requires storing bucket state (tokens + timestamp)
    - Can be less intuitive to configure
    - Burst allowance may not be desired in all cases

    **When to use:**
    - APIs with variable traffic patterns
    - When users need occasional burst capacity
    - Mobile apps (handle network reconnects)
    - Background jobs that batch requests
    - General purpose rate limiting with flexibility

    **Storage format:**
    - Key: `{namespace}:{key}:tokenbucket`
    - Value: JSON `{"tokens": float, "last_refill": timestamp_ms}`
    - TTL: 2x refill period for safety

    **Configuration:**
    - `burst_size`: Maximum bucket capacity (default = rate.limit)
        - Set higher to allow larger bursts (e.g., burst_size=200, limit=100)
        - Set equal to limit for no burst allowance

    **Algorithm:**
    - Refill rate = limit / expire (e.g., 100 tokens / 60000ms = 0.00167 tokens/ms)
    - Lazy refill: tokens calculated on-demand, not background process
    - Formula: `tokens = min(tokens + (elapsed_time * refill_rate), capacity)`

    **Example:**
    ```python
    from traffik.rates import Rate
    from traffik.strategies import TokenBucketStrategy

    # Allow 100 requests/minute with burst up to 150
    rate = Rate.parse("100/1m")
    strategy = TokenBucketStrategy(burst_size=150)
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = int(wait_ms / 1000)
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```

    **Real-world scenario:**
    User makes 150 requests instantly (burst):
    - First 100 requests: Consume 100 tokens, bucket now has 50 tokens
    - Next 50 requests: Consume 50 tokens, bucket now empty
    - Request 151: Must wait ~0.6s for next token to refill
    aults to rate.limit
    (no burst allowance beyond rate limit).   - After 30s: Bucket has refilled 50 tokens, can burst again

    """

    burst_size: typing.Optional[int] = None
    """Maximum bucket capacity (positive tokens). If None, defaults to `rate.limit`."""
    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]  # type: ignore[arg-type]
    """Configuration for backend locking during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply Token Bucket rate limiting.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        refill_period_ms = rate.expire
        refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit

        full_key = backend.get_key(str(key))
        bucket_key = f"{full_key}:tokenbucket:{capacity}"
        ttl_seconds = max(
            int((refill_period_ms * 2) // 1000), 1
        )  # 2x refill period for safety, at least 1s

        async with await backend.lock(f"lock:{bucket_key}", **self.lock_config):
            old_state_json = await backend.get(bucket_key)
            # If state exists, load tokens and last refill time
            if old_state_json and old_state_json != "":
                try:
                    bucket_state: typing.Dict[str, typing.Any] = load_data(
                        old_state_json
                    )
                    tokens = float(bucket_state.get("tokens", capacity))
                    last_refill = float(bucket_state.get("last_refill", now))
                except (MsgPackDecodeError, ValueError, KeyError, AttributeError):
                    # If state is corrupted, reinitialize bucket at full capacity
                    tokens = float(capacity)
                    last_refill = now
            else:
                # If no existing state, initialize bucket at full capacity
                tokens = float(capacity)
                last_refill = now

            # Calculate tokens to refill based on elapsed time
            time_elapsed = now - last_refill
            tokens_to_add = refill_rate * time_elapsed
            tokens = min(tokens + tokens_to_add, float(capacity))

            # If bucket has enough tokens for the cost, allow request
            if tokens >= cost:
                # Consume cost tokens from bucket
                tokens -= cost
                new_state = dump_data({"tokens": tokens, "last_refill": now})
                await backend.set(bucket_key, new_state, expire=ttl_seconds)
                return 0.0

            # If not enough tokens, calculate wait time for required tokens
            tokens_needed = cost - tokens
            wait_ms = tokens_needed / refill_rate

            # Save current state without consuming tokens
            new_state = dump_data({"tokens": tokens, "last_refill": now})
            await backend.set(bucket_key, new_state, expire=ttl_seconds)
            return wait_ms

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[TokenBucketStatMetadata]:
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
                wait_ms=0.0,
            )

        now = time() * 1000
        refill_period_ms = rate.expire
        refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit

        full_key = backend.get_key(str(key))
        bucket_key = f"{full_key}:tokenbucket:{capacity}"

        old_state_json = await backend.get(bucket_key)
        # If state exists, load tokens and last refill time
        if old_state_json and old_state_json != "":
            try:
                bucket_state: typing.Dict[str, typing.Any] = load_data(old_state_json)
                tokens = float(bucket_state.get("tokens", capacity))
                last_refill = float(bucket_state.get("last_refill", now))
            except (MsgPackDecodeError, ValueError, KeyError, AttributeError):
                # If state is corrupted, assume bucket is at full capacity
                tokens = float(capacity)
                last_refill = now
        else:
            # If no existing state, bucket is at full capacity
            tokens = float(capacity)
            last_refill = now

        # Calculate current tokens after refilling
        time_elapsed = now - last_refill
        tokens_to_add = refill_rate * time_elapsed
        tokens = min(tokens + tokens_to_add, float(capacity))

        # Hits remaining is the current token count
        hits_remaining = max(tokens, 0.0)

        # If tokens are negative (shouldn't happen but safe guard), calculate wait time
        if tokens < 0:
            wait_ms = abs(tokens) / refill_rate
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=TokenBucketStatMetadata(
                strategy="token_bucket",
                tokens=tokens,
                capacity=capacity,
                refill_rate_per_ms=refill_rate,
                last_refill_ms=last_refill,
            ),
        )


@dataclass(frozen=True)
class TokenBucketWithDebtStrategy:
    """
    Token Bucket with Debt (allows temporary overdraft).

    Enhanced token bucket that allows going into "debt" (negative token balance).
    Requests can exceed burst capacity temporarily, with debt paid back over time
    through normal token refill. Provides smoother user experience during spikes.

    **How it works:**
    1. Works like standard token bucket with token refill
    2. When bucket has < 1 token, can still allow requests if within debt limit
    3. Tokens can go negative (e.g., -20 tokens = 20 in debt)
    4. Refilled tokens first pay back debt before allowing new requests
    5. Prevents requests only when debt limit exceeded

    **Pros:**
    - More forgiving than standard token bucket
    - Smoother user experience during traffic spikes
    - Good for variable/unpredictable traffic patterns
    - Allows temporary over-limit without hard cutoff
    - Self-correcting (debt paid back automatically)

    **Cons:**
    - More complex to reason about
    - Can allow sustained overuse if debt limit too high
    - Requires careful tuning of debt limit
    - May violate strict rate requirements

    **When to use:**
    - User-facing APIs where hard limits feel harsh
    - Mobile apps with unreliable connectivity
    - Gradual degradation preferred over hard cutoff
    - When occasional over-limit is acceptable
    - Development/testing environments

    **When NOT to use:**
    - APIs with strict rate requirements
    - Third-party API proxying (must respect their limits)
    - Billing/payment systems
    - Security-critical endpoints

    **Storage format:**
    - Key: `{namespace}:{key}:tokenbucket:debt`
    - Value: JSON `{"tokens": float, "last_refill": timestamp_ms}`
    - Note: tokens can be negative (debt)
    - TTL: 2x refill period for safety

    **Configuration:**
    - `burst_size`: Maximum bucket capacity (positive tokens)
    - `max_debt`: Maximum negative tokens allowed (overdraft limit)
        - Example: burst_size=100, max_debt=50 means -50 to +100 token range

    **Algorithm:**
    - Same refill as standard token bucket
    - Allow request if: `tokens - 1.0 >= -max_debt` (still have debt capacity after consuming)
    - Tokens can range from `-max_debt` to `+burst_size`

    **Example:**
    ```python
    from traffik.rates import Rate
    from traffik.strategies import TokenBucketWithDebtStrategy

    # 100 req/min, burst=150, allow 50 requests of debt
    rate = Rate.parse("100/m")
    strategy = TokenBucketWithDebtStrategy(burst_size=150, max_debt=50)
    wait_ms = await strategy("user:123", rate, backend)

    if wait_ms > 0:
        wait_seconds = int(wait_ms / 1000)
        raise HTTPException(429, f"Rate limited. Retry in {wait_seconds}s")
    ```

    **Real-world scenario:**
    User makes 200 requests instantly:
    - Requests 1-150: Consume 150 tokens (burst), bucket at 0
    - Requests 151-200: Go into debt, bucket at -50 (max debt)
    - Request 201: Rejected (debt limit hit)
    - Next 30s: Refill 50 tokens, pay back debt to 0
    - After 60s: Bucket back to 50 tokens, user can request again

    Compare to standard token bucket:
    - Would reject after request 150 (no debt allowance)
    - User experience: "Why did it suddenly stop working?"

    """

    burst_size: typing.Optional[int] = None
    """Maximum bucket capacity (positive tokens). If None, defaults to `rate.limit`."""
    max_debt: int = 0
    """Maximum negative tokens allowed (overdraft limit). Set to 0 for standard behavior."""
    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        if self.burst_size is not None and self.burst_size < 0:
            raise ValueError("burst_size must be non-negative")

        if self.max_debt < 0:
            raise ValueError("max_debt must be non-negative")

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        """
        Apply Token Bucket with Debt rate limiting.

        :param key: The throttling key (e.g., user ID, IP address).
        :param rate: The rate limit definition.
        :param backend: The throttle backend instance.
        :param cost: The cost/weight of this request (default: 1).
        :return: Wait time in milliseconds if throttled, 0.0 if allowed.
        """
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        refill_period_ms = rate.expire
        refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit
        max_debt = self.max_debt

        full_key = backend.get_key(str(key))
        bucket_key = f"{full_key}:tokenbucket:{capacity}:debt:{max_debt}"
        ttl_seconds = max(
            int((refill_period_ms * 2) // 1000), 1
        )  # 2x refill period for safety, at least 1s

        async with await backend.lock(f"lock:{bucket_key}", **self.lock_config):
            old_state_json = await backend.get(bucket_key)
            # If state exists, load tokens and last refill time
            if old_state_json and old_state_json != "":
                try:
                    bucket_state = load_data(old_state_json)
                    tokens = float(bucket_state.get("tokens", capacity))
                    last_refill = float(bucket_state.get("last_refill", now))
                except (MsgPackDecodeError, ValueError, KeyError, AttributeError):
                    # If state is corrupted, reinitialize bucket at full capacity
                    tokens = float(capacity)
                    last_refill = now
            else:
                # If no existing state, initialize bucket at full capacity
                tokens = float(capacity)
                last_refill = now

            # Calculate tokens to refill based on elapsed time
            time_elapsed = now - last_refill
            tokens_to_add = refill_rate * time_elapsed
            tokens = min(tokens + tokens_to_add, float(capacity))

            # If consuming cost tokens would still be within debt limit, allow request
            if tokens - cost >= -max_debt:
                # Allow request and consume cost tokens (may go negative)
                tokens -= cost
                new_state = dump_data({"tokens": tokens, "last_refill": now})
                await backend.set(bucket_key, new_state, expire=ttl_seconds)
                return 0.0

            # If consuming would exceed debt limit, calculate wait time
            tokens_needed = -max_debt - tokens + cost
            wait_ms = tokens_needed / refill_rate

            # Save current state without consuming tokens
            new_state = dump_data({"tokens": tokens, "last_refill": now})
            await backend.set(bucket_key, new_state, expire=ttl_seconds)
            return wait_ms

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[TokenBucketWithDebtStatMetadata]:
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
                wait_ms=0.0,
            )

        now = time() * 1000
        refill_period_ms = rate.expire
        refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit
        max_debt = self.max_debt

        full_key = backend.get_key(str(key))
        bucket_key = bucket_key = f"{full_key}:tokenbucket:{capacity}:debt:{max_debt}"

        old_state_json = await backend.get(bucket_key)
        # If state exists, load tokens and last refill time
        if old_state_json and old_state_json != "":
            try:
                bucket_state = load_data(old_state_json)
                tokens = float(bucket_state.get("tokens", capacity))
                last_refill = float(bucket_state.get("last_refill", now))
            except (MsgPackDecodeError, ValueError, KeyError, AttributeError):
                # If state is corrupted, assume bucket is at full capacity
                tokens = float(capacity)
                last_refill = now
        else:
            # If no existing state, bucket is at full capacity
            tokens = float(capacity)
            last_refill = now

        # Calculate current tokens after refilling
        time_elapsed = now - last_refill
        tokens_to_add = refill_rate * time_elapsed
        tokens = min(tokens + tokens_to_add, float(capacity))

        # Hits remaining includes debt allowance: tokens can go to -max_debt
        hits_remaining = max(tokens + max_debt, 0.0)

        # If tokens are below negative debt limit, calculate wait time
        if tokens < -max_debt:
            tokens_needed = -max_debt - tokens
            wait_ms = tokens_needed / refill_rate
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=TokenBucketWithDebtStatMetadata(
                strategy="token_bucket_with_debt",
                tokens=tokens,
                capacity=capacity,
                max_debt=max_debt,
                current_debt=max(-tokens, 0.0) if tokens < 0 else 0.0,
                refill_rate_per_ms=refill_rate,
                last_refill_ms=last_refill,
            ),
        )


TokenBucket = TokenBucketStrategy  # Alias for convenience
TokenBucketWithDebt = TokenBucketWithDebtStrategy  # Alias for convenience
