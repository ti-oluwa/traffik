"""
Advanced Rate Limiting Strategies for Special Use Cases
"""

import heapq
import time as pytime
import typing
from dataclasses import dataclass, field
from enum import IntEnum

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, Stringable, WaitPeriod
from traffik.utils import (
    MsgPackDecodeError,
    dump_data,
    get_blocking_setting,
    get_blocking_timeout,
    load_data,
    time,
)

__all__ = [
    "TieredRateStrategy",
    "AdaptiveThrottleStrategy",
    "PriorityQueueStrategy",
    "QuotaWithRolloverStrategy",
    "TimeOfDayStrategy",
    "CostBasedTokenBucketStrategy",
    "GCRAStrategy",
    "DistributedFairnessStrategy",
    "GeographicDistributionStrategy",
]


@dataclass(frozen=True)
class TieredRateStrategy:
    """
    Fixed window tiered rate limiting with different limits per user tier.

    **Use case:** SaaS applications with free/premium/enterprise tiers

    **How it works:**
    - Extract tier from key (e.g., "tier:premium:user:123")
    - Apply tier-specific rate multiplier
    - Enterprise users get 10x free tier limit, premium 5x, etc.

    **Example:**
    ```python
    # Base rate: 100/hour
    # Free: 100/hour, Premium: 500/hour, Enterprise: 1000/hour
    strategy = TieredRateStrategy(
        tier_multipliers={"free": 1.0, "premium": 5.0, "enterprise": 10.0},
        default_tier="free"
    )

    # Identifier should format as "tier:{tier}:user:{id}"
    async def tier_identifier(connection):
        user = extract_user(connection)
        return f"tier:{user.tier}:user:{user.id}"

    throttle = HTTPThrottle(
        uid="api",
        rate="100/hour",  # Base rate for free tier
        strategy=strategy,
        identifier=tier_identifier,
    )
    ```

    **Storage:**
    - `{key}:tiered:{tier}:{window}` - Counter per tier per window
    """

    tier_multipliers: typing.Dict[str, float] = field(
        default_factory=lambda: {
            "free": 1.0,
            "premium": 5.0,
            "enterprise": 10.0,
        }
    )
    """Multipliers for each tier relative to base rate"""

    default_tier: str = "free"
    """Default tier if not specified in key"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""
    marker: str = "tier:"
    """
    Marker used in keys to identify tier segment.
    
    For example, with marker "tier:", in key "...:tier:premium:...", "premium" is the tier.
    """

    def __post_init__(self) -> None:
        if not self.tier_multipliers:
            raise ValueError("`tier_multipliers` cannot be empty")

        if self.default_tier not in self.tier_multipliers:
            raise ValueError(
                "`default_tier` must be one of the keys in `tier_multipliers`"
            )

        if not all(isinstance(v, float) for v in self.tier_multipliers.values()):
            raise ValueError("All tier multipliers must be numbers")

        if not self.marker.endswith(":"):
            object.__setattr__(self, "marker", self.marker + ":")

    def _get_tier(self, key: str) -> str:
        """Extract tier from key format: 'tier:{tier}:...'"""
        marker = self.marker
        if marker not in key:
            return self.default_tier

        parts = key.split(marker)
        if len(parts) < 2:
            return self.default_tier

        tier_part = parts[1]
        tier = tier_part.split(":")[0]
        return tier or self.default_tier

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        # Extract tier and calculate effective limit
        tier = self._get_tier(str(key))
        multiplier = self.tier_multipliers.get(tier, 1.0)
        effective_limit = int(rate.limit * multiplier)

        now = time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:tiered:{tier}:{current_window}"
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        # Use fixed window counting with tier-specific limit
        count = await backend.increment_with_ttl(
            counter_key, amount=cost, ttl=ttl_seconds
        )
        if count > effective_limit:
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            return max(wait_ms, 0.0)

        return 0.0


@dataclass(frozen=True)
class AdaptiveThrottleStrategy:
    """
    Adaptive rate limiting that adjusts based on backend load/health.

    **Use case:** Protect backend from overload while maximizing throughput

    **How it works:**
    - Monitor current load percentage (requests / limit)
    - When load > threshold, reduce effective limit
    - Gradually recover as load decreases

    **Example:**
    ```python
    strategy = AdaptiveThrottleStrategy(
        load_threshold=0.8,        # Start throttling at 80% capacity
        reduction_factor=0.6,      # Reduce to 60% of normal limit
        recovery_rate=0.1,         # Recover 10% per window
    )

    throttle = HTTPThrottle(
        uid="adaptive_api",
        rate="1000/hour",  # Can adapt down to ~600/hour under load
        strategy=strategy,
    )
    ```

    **Benefits:**
    - Prevents thundering herd
    - Smooths traffic spikes
    - Self-adjusting based on actual load

    **Storage:**
    - `{key}:adaptive:{window}:counter` - Request counter
    - `{key}:adaptive:{window}:limit` - Current effective limit
    """

    load_threshold: float = 0.8
    """Load percentage that triggers throttling (0.0-1.0)"""

    reduction_factor: float = 0.6
    """Reduce limit to this fraction during high load"""

    recovery_rate: float = 0.1
    """Rate at which limit recovers (per window)"""

    min_limit_ratio: float = 0.3
    """Never go below this ratio of base limit"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    def __post_init__(self) -> None:
        if not (0.0 < self.load_threshold < 1.0):
            raise ValueError("`load_threshold` must be between 0.0 and 1.0")

        if not (0.0 < self.reduction_factor < 1.0):
            raise ValueError("`reduction_factor` must be between 0.0 and 1.0")

        if not (0.0 < self.recovery_rate < 1.0):
            raise ValueError("`recovery_rate` must be between 0.0 and 1.0")

        if not (0.0 < self.min_limit_ratio < 1.0):
            raise ValueError("`min_limit_ratio` must be between 0.0 and 1.0")

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:adaptive:{current_window}:counter"
        limit_key = f"{full_key}:adaptive:{current_window}:limit"
        prev_limit_key = f"{full_key}:adaptive:{current_window - 1}:limit"
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        async with await backend.lock(f"lock:{counter_key}", **self.lock_config):
            # Increment counter
            count = await backend.increment_with_ttl(
                counter_key, amount=cost, ttl=ttl_seconds
            )

            # Get or initialize effective limit
            limit_str = await backend.get(limit_key)
            if limit_str:
                effective_limit = float(limit_str)
            else:
                # Check previous window's limit for continuity
                prev_limit_str = await backend.get(prev_limit_key)
                if prev_limit_str:
                    prev_limit = float(prev_limit_str)
                    # Recover slightly from previous limit
                    effective_limit = min(
                        rate.limit, prev_limit + (rate.limit * self.recovery_rate)
                    )
                else:
                    effective_limit = float(rate.limit)

                await backend.set(limit_key, str(effective_limit), expire=ttl_seconds)

            # Calculate current load
            load = count / effective_limit if effective_limit > 0 else 0

            # Adjust limit if needed
            if load > self.load_threshold:
                # if load is high, reduce limit
                new_limit = max(
                    rate.limit * self.min_limit_ratio,
                    effective_limit * self.reduction_factor,
                )
                await backend.set(limit_key, str(new_limit), expire=ttl_seconds)
                effective_limit = new_limit

            # If request exceeds effective limit, calculate wait
            if count > effective_limit:
                time_in_window = now % window_duration_ms
                wait_ms = window_duration_ms - time_in_window
                return max(wait_ms, 0.0)
            return 0.0


class Priority(IntEnum):
    """Request priority levels"""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class PriorityQueueStrategy:
    """
    Rate limiting with priority queue (FIFO ordering within priority).

    **Use case:** APIs where some requests should be processed preferentially

    **How it works:**
    - Maintain queue of `[timestamp, priority, cost]` tuples
    - Process high-priority requests first
    - Lower-priority requests wait longer when at capacity

    **Example:**
    ```python
    strategy = PriorityQueueStrategy(
        default_priority=Priority.NORMAL,
        max_queue_size=1000,  # Prevent unbounded growth
    )

    # Identifier encodes priority: "priority:{level}:user:{id}"
    async def priority_identifier(connection):
        user = extract_user(connection)
        priority = connection.headers.get("X-Priority", "2")
        return f"priority:{priority}:user:{user.id}"

    throttle = HTTPThrottle(
        uid="priority_api",
        rate="100/minute",
        strategy=strategy,
        identifier=priority_identifier,
    )
    ```

    **Use cases:**
    - Admin/system requests (CRITICAL)
    - Paid user requests (HIGH)
    - Free user requests (NORMAL)
    - Batch/background jobs (LOW)

    **Storage:**
    - `{key}:priority:queue` - JSON array of `[timestamp, priority, cost]`
    """

    default_priority: Priority = Priority.NORMAL
    """Default priority if not specified"""

    max_queue_size: int = 1000
    """Maximum queue size (prevents memory issues)"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    marker: str = "priority:"
    """
    Marker used in keys to identify priority segment.

    For example, with marker "priority:", in key "...:priority:3:...", "3" is the priority level.
    """

    def __post_init__(self) -> None:
        if self.max_queue_size <= 0:
            raise ValueError("`max_queue_size` must be a positive integer")

        if not self.marker.endswith(":"):
            object.__setattr__(self, "marker", self.marker + ":")

    def _get_priority(self, key: str) -> Priority:
        """Extract priority from key format: 'priority:{level}:...'"""
        if "priority:" in key:
            parts = key.split("priority:")
            if len(parts) >= 2:
                level_part = parts[1]
                try:
                    level = int(level_part.split(":")[0])
                    return Priority(level)
                except (ValueError, IndexError):
                    return self.default_priority
        return self.default_priority

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        priority = self._get_priority(str(key))
        full_key = backend.get_key(str(key))
        queue_key = f"{full_key}:priority:queue"
        ttl_seconds = max(int(rate.expire // 1000), 1)

        async with await backend.lock(f"lock:{queue_key}", **self.lock_config):
            # Get current queue
            queue_json = await backend.get(queue_key)
            if queue_json:
                try:
                    queue = load_data(queue_json)
                except MsgPackDecodeError:
                    queue = []
            else:
                queue = []

            # Remove expired entries
            cutoff = now - rate.expire
            queue = [[ts, pri, c] for ts, pri, c in queue if ts > cutoff]

            # Calculate total cost of higher/equal priority requests
            higher_priority_cost = sum(c for _, pri, c in queue if pri >= priority)

            # Check if we can accept this request
            if higher_priority_cost + cost > rate.limit:
                # Calculate wait time based on oldest high-priority request
                high_priority_entries = [ts for ts, pri, _ in queue if pri >= priority]
                if high_priority_entries:
                    oldest = min(high_priority_entries)
                    wait_ms = rate.expire - (now - oldest)
                    return max(wait_ms, 0.0)
                return rate.expire  # Shouldn't reach here

            # Add current request to queue
            queue.append([now, priority, cost])

            # Enforce max queue size (remove oldest low priority if needed)
            if len(queue) > self.max_queue_size:
                # Keep the highest priority items. Also keep newest items within same priority (negative timestamp for max-heap behavior)
                queue = heapq.nlargest(
                    self.max_queue_size,
                    queue,
                    key=lambda x: (
                        x[1],
                        -x[0],
                    ),  # Sort by: (priority DESC, timestamp DESC)
                )

            # Save updated queue
            await backend.set(queue_key, dump_data(queue), expire=ttl_seconds)
            return 0.0


@dataclass(frozen=True)
class QuotaWithRolloverStrategy:
    """
    Quota-based rate limiting with rollover of unused quota.

    **Use case:** Monthly API quotas where unused quota should not be wasted

    **How it works:**
    - Track quota usage over period (e.g., month)
    - Unused quota rolls over to next period (up to max)
    - Prevents "use it or lose it" wastage

    **Example:**
    ```python
    strategy = QuotaWithRolloverStrategy(
        rollover_percentage=0.5,  # Roll over 50% of unused quota
        max_rollover=500,          # Max 500 requests can roll over
    )

    throttle = HTTPThrottle(
        uid="monthly_quota",
        rate="1000/30days",  # 1000 per month
        strategy=strategy,
    )
    ```

    **Use cases:**
    - Monthly API quotas
    - Credit-based systems
    - Subscription limits

    **Storage:**
    - `{key}:quota:{period}:used` - Used quota this period
    - `{key}:quota:{period}:rollover` - Rolled over from previous period
    """

    rollover_percentage: float = 0.5
    """Percentage of unused quota to roll over (0.0-1.0)"""

    max_rollover: int = 500
    """Maximum requests that can roll over"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    def __post_init__(self) -> None:
        if not (0.0 <= self.rollover_percentage <= 1.0):
            raise ValueError("`rollover_percentage` must be between 0.0 and 1.0")
        if self.max_rollover < 0:
            raise ValueError("`max_rollover` must be non-negative")

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire
        current_period = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        used_key = f"{full_key}:quota:{current_period}:used"
        rollover_key = f"{full_key}:quota:{current_period}:rollover"
        prev_used_key = f"{full_key}:quota:{current_period - 1}:used"
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        async with await backend.lock(f"lock:{used_key}", **self.lock_config):
            # Get current usage
            used_str = await backend.get(used_key)
            used = int(used_str) if used_str else 0

            # Get or calculate rollover for this period
            rollover_str = await backend.get(rollover_key)
            if rollover_str:
                rollover = int(rollover_str)
            else:
                # Calculate rollover from previous period
                prev_used_str = await backend.get(prev_used_key)
                if prev_used_str:
                    prev_used = int(prev_used_str)
                    unused = max(0, rate.limit - prev_used)
                    rollover = min(
                        self.max_rollover, int(unused * self.rollover_percentage)
                    )
                else:
                    rollover = 0

                await backend.set(rollover_key, str(rollover), expire=ttl_seconds)

            # Effective limit includes rollover
            effective_limit = rate.limit + rollover

            # Check if request exceeds limit
            if used + cost > effective_limit:
                time_in_period = now % window_duration_ms
                wait_ms = window_duration_ms - time_in_period
                return max(wait_ms, 0.0)

            # Increment usage
            await backend.increment_with_ttl(used_key, amount=cost, ttl=ttl_seconds)
            return 0.0


@dataclass(frozen=True)
class TimeOfDayStrategy:
    """
    Fixed window rate limiting with different limits based on time of day.

    **Use case:** Peak vs off-peak pricing, business hours enforcement

    **How it works:**
    - Define time windows with different rate multipliers
    - Apply multiplier based on current hour (UTC)
    - E.g., 2x limit during business hours, 1x at night

    Note: The time window boundaries are inclusive of the start hour and exclusive of the end hour.
    Time windows should be defined in 24-hour format (0-24).

    **Example:**
    ```python
    strategy = TimeOfDayStrategy(
        time_windows=[
            # (start_hour, end_hour, multiplier)
            (0, 6, 2.0),    # Night: 2x limit (200/hour)
            (6, 18, 1.0),   # Day: 1x limit (100/hour)
            (18, 24, 1.5),  # Evening: 1.5x limit (150/hour)
        ],
        timezone_offset=0,  # UTC offset in hours
    )

    throttle = HTTPThrottle(
        uid="tod_api",
        rate="100/hour",  # Base rate
        strategy=strategy,
    )
    ```

    **Storage:**
    - `{key}:tod:{window_id}:counter` - Counter per time window
    """

    time_windows: typing.List[typing.Tuple[int, int, float]] = field(
        default_factory=lambda: [
            (0, 8, 2.0),  # Night (00:00-08:00): 2x
            (8, 17, 1.0),  # Business hours (08:00-17:00): 1x
            (17, 24, 1.5),  # Evening (17:00-00:00): 1.5x
        ]
    )
    """
    List of (start_hour, end_hour, multiplier) tuples (24-hour format)
    
    Best practice is that the time windows should cover the full 24-hour period without gaps.
    
    For example:
    ```json
    [(0, 8, 2.0), (8, 17, 1.0), (17, 24, 1.5)]
    ```
    Means:
    - From 00:00 to 08:00, apply a 2.0x multiplier
    - From 08:00 to 17:00, apply a 1.0x multiplier
    - From 17:00 to 24:00, apply a 1.5x multiplier
    """

    timezone_offset: int = 0
    """Timezone offset from UTC in hours. Time offset can range from -12 to +14."""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    def __post_init__(self) -> None:
        if not self.time_windows:
            raise ValueError("`time_windows` cannot be empty")

        for start, end, mult in self.time_windows:
            if not (0 <= start < 24) or not (0 < end <= 24):
                raise ValueError(
                    "Start and end hours must be in 0-24 range (24 exclusive for start)"
                )
            if start >= end:
                raise ValueError("Start hour must be less than end hour")
            if mult <= 0:
                raise ValueError("Multiplier must be positive")

        if self.timezone_offset < -12 or self.timezone_offset > 14:
            raise ValueError("`timezone_offset` must be between -12 and +14 hours")

    def _get_current_multiplier(self, timestamp_ms: float) -> float:
        """Get rate multiplier for current time"""
        # Convert to hours since epoch, adjust for timezone
        hours_since_epoch = (timestamp_ms / 1000 / 3600) + self.timezone_offset
        hour_of_day = int(hours_since_epoch % 24)

        # Find matching time window
        for start_hour, end_hour, multiplier in self.time_windows:
            if start_hour <= hour_of_day < end_hour:
                return multiplier

        # Default multiplier if no match
        return 1.0

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        # We must use wall clock time for time-of-day calculations, not event loop time
        now = pytime.time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        # Get current multiplier
        multiplier = self._get_current_multiplier(now)
        effective_limit = int(rate.limit * multiplier)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:tod:{current_window}:counter"
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        count = await backend.increment_with_ttl(
            counter_key, amount=cost, ttl=ttl_seconds
        )
        if count > effective_limit:
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            return max(wait_ms, 0.0)
        return 0.0


@dataclass(frozen=True)
class CostBasedTokenBucketStrategy:
    """
    Token bucket that refills based on cost consumption patterns.

    **Use case:** APIs where different operations have different costs

    **How it works:**
    - Track average cost of recent requests
    - Refill rate adjusts based on cost pattern
    - Expensive operations slow down refill temporarily

    **Example:**
    ```python
    strategy = CostBasedTokenBucketStrategy(
        burst_size=200,
        cost_window=100,  # Track last 100 requests for average
    )

    throttle = HTTPThrottle(
        uid="cost_api",
        rate="100/minute",
        strategy=strategy,
    )

    # Usage with dynamic costs
    await throttle(request, cost=1)   # Simple read
    await throttle(request, cost=10)  # Complex query
    await throttle(request, cost=50)  # Report generation
    ```

    **Storage:**
    - `{key}:costbucket:state` - `{"tokens": float, "last_refill": ts}`
    - `{key}:costbucket:history` - Recent costs for average calculation
    """

    burst_size: typing.Optional[int] = None
    """Maximum bucket capacity (defaults to rate.limit)"""

    cost_window: int = 100
    """Number of recent requests to track for cost averaging"""

    min_refill_rate: float = 0.5
    """Minimum refill rate multiplier (prevents starvation)"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        refill_period_ms = rate.expire
        base_refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit

        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:costbucket:state"
        history_key = f"{full_key}:costbucket:history"
        ttl_seconds = max(int((refill_period_ms * 2) // 1000), 1)

        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            # Get bucket state
            state_json = await backend.get(state_key)
            if state_json:
                try:
                    state = load_data(state_json)
                    tokens = float(state["tokens"])
                    last_refill = float(state["last_refill"])
                except (MsgPackDecodeError, KeyError, ValueError):
                    tokens = float(capacity)
                    last_refill = now
            else:
                tokens = float(capacity)
                last_refill = now

            # Get cost history
            history_json = await backend.get(history_key)
            if history_json:
                try:
                    history = load_data(history_json)
                except MsgPackDecodeError:
                    history = []
            else:
                history = []

            # Calculate average cost
            if history:
                avg_cost = sum(history) / len(history)
                # Adjust refill rate based on average cost
                # Higher average cost = slower refill
                cost_multiplier = max(self.min_refill_rate, 1.0 / avg_cost)
                effective_refill_rate = base_refill_rate * cost_multiplier
            else:
                effective_refill_rate = base_refill_rate

            # Refill tokens
            time_elapsed = now - last_refill
            tokens_to_add = effective_refill_rate * time_elapsed
            tokens = min(tokens + tokens_to_add, float(capacity))

            # Check if enough tokens
            if tokens >= cost:
                # Consume tokens
                tokens -= cost

                # Update history
                history.append(cost)
                if len(history) > self.cost_window:
                    history = history[-self.cost_window :]

                # Save state
                new_state = {"tokens": tokens, "last_refill": now}
                await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
                await backend.set(history_key, dump_data(history), expire=ttl_seconds)
                return 0.0

            # Calculate wait time for required tokens
            tokens_needed = cost - tokens
            wait_ms = tokens_needed / effective_refill_rate

            # Save current state
            new_state = {"tokens": tokens, "last_refill": now}
            await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
            return wait_ms


@dataclass(frozen=True)
class GCRAStrategy:
    """
    GCRA (Generic Cell Rate Algorithm) for perfectly smooth rate limiting.

    Also known as "leaky bucket as meter" or "virtual scheduling algorithm".
    Provides the smoothest possible rate limiting with zero bursts.

    **Use case:** When you need perfectly smooth traffic distribution

    **How it works:**
    - Maintains TAT (Theoretical Arrival Time) for next allowed request
    - Each request increments TAT by emission interval
    - Request allowed if: current_time >= TAT - burst_tolerance
    - Update TAT: max(TAT, current_time) + emission_interval * cost

    **Example:**
    ```python
    # 100 req/min = 600ms between requests
    strategy = GCRAStrategy(
        burst_tolerance_ms=0,  # Perfectly smooth, no bursts
    )

    throttle = HTTPThrottle(
        uid="smooth_api",
        rate="100/minute",
        strategy=strategy,
    )
    ```

    **Benefits over token bucket:**
    - More memory efficient (stores single TAT value)
    - Perfectly smooth distribution
    - No burst capacity exploitation
    - Simpler algorithm

    **When to use:**
    - Telecommunications systems
    - Strict SLA enforcement
    - Real-time processing pipelines
    - Preventing sudden spikes

    **Storage:**
    - `{key}:gcra:tat` - Theoretical arrival time (milliseconds)
    """

    burst_tolerance_ms: float = 0.0
    """How much burst to tolerate (0 = perfectly smooth). Can be any non-negative value."""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    def __post_init__(self) -> None:
        if self.burst_tolerance_ms < 0:
            raise ValueError("`burst_tolerance_ms` must be non-negative")

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        emission_interval = rate.expire / rate.limit  # ms between requests

        full_key = backend.get_key(str(key))
        tat_key = f"{full_key}:gcra:tat"
        ttl_seconds = max(int((rate.expire * 2) // 1000), 1)

        async with await backend.lock(f"lock:{tat_key}", **self.lock_config):
            # Get current TAT
            tat_str = await backend.get(tat_key)
            tat = float(tat_str) if tat_str else now

            # Calculate new TAT
            new_tat = max(tat, now) + (emission_interval * cost)

            # Check if request is conformant
            if now >= (tat - self.burst_tolerance_ms):
                # Allowed - update TAT
                await backend.set(tat_key, str(new_tat), expire=ttl_seconds)
                return 0.0

            # Denied. Calculate wait time
            wait_ms = tat - now
            return max(wait_ms, 0.0)


@dataclass(frozen=True)
class DistributedFairnessStrategy:
    """
    Distributed fair queuing using deficit round-robin.

    Ensures fair distribution of rate limit across multiple application instances.
    Prevents any single instance from hogging the shared rate limit.

    **Use case:** Multi-instance deployments with shared Redis backend

    **How it works:**
    - Each app instance gets equal share of global limit
    - Uses deficit counter to handle fractional shares
    - Instances that underutilize donate capacity to others
    - Weighted fair queuing for priority instances

    **Example:**
    ```python
    import socket

    strategy = DistributedFairnessStrategy(
        instance_id=socket.gethostname(),  # Unique per instance
        instance_weight=1.0,                # Equal weight
        fairness_window_ms=60000,          # 1 minute fairness window
    )

    throttle = HTTPThrottle(
        uid="distributed_api",
        rate="1000/minute",  # Shared across all instances
        strategy=strategy,
        backend=RedisBackend("redis://shared:6379"),
    )
    ```

    **Scenario:**
    - 3 instances, 900 req/min limit
    - Each gets: 300 req/min quota
    - Instance A only uses 200 → donates 100
    - Instances B & C can use extra capacity

    **Storage:**
    - `{key}:dfq:instances` - Active instance registry
    - `{key}:dfq:usage:{instance}` - Per-instance usage
    - `{key}:dfq:deficit:{instance}` - Deficit counter
    """

    instance_id: str
    """Unique identifier for this application instance"""

    instance_weight: float = 1.0
    """Weight for weighted fair queuing (higher = more quota)"""

    fairness_window_ms: float = 60000
    """
    Window for fairness calculation (1 minute). 
    
    Fairness is calculated per this interval. Think of it as the "round duration" for
    deficit round-robin.

    60000 ms = 1 minute is typical.
    10000 ms = 10 seconds for more responsive balancing.
    300000 ms = 5 minutes for very stable balancing.
    0 ms is not allowed.
    """

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=True,
            blocking_timeout=0.2,
        )
    )

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("`instance_id` must be a non-empty string")

        if self.instance_weight <= 0:
            raise ValueError("`instance_weight` must be a positive number")

        if self.fairness_window_ms <= 0:
            raise ValueError("`fairness_window_ms` must be a positive number")

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        current_window = int(now // self.fairness_window_ms)

        full_key = backend.get_key(str(key))
        instances_key = f"{full_key}:dfq:instances"
        usage_key = f"{full_key}:dfq:usage:{self.instance_id}:{current_window}"
        deficit_key = f"{full_key}:dfq:deficit:{self.instance_id}:{current_window}"
        global_usage_key = f"{full_key}:dfq:global:{current_window}"
        ttl_seconds = max(int((self.fairness_window_ms * 2) // 1000), 1)

        async with await backend.lock(f"lock:{instances_key}", **self.lock_config):
            # Register this instance
            instances_json = await backend.get(instances_key)
            if instances_json:
                try:
                    instances = load_data(instances_json)
                except MsgPackDecodeError:
                    instances = {}
            else:
                instances = {}

            instances[self.instance_id] = {
                "weight": self.instance_weight,
                "last_seen": now,
            }

            # Remove stale instances (not seen in 2 windows)
            stale_threshold = now - (self.fairness_window_ms * 2)
            instances = {
                iid: data
                for iid, data in instances.items()
                if data["last_seen"] > stale_threshold
            }
            await backend.set(instances_key, dump_data(instances), expire=ttl_seconds)

            # Calculate fair share
            total_weight = sum(data["weight"] for data in instances.values())
            fair_share = int((rate.limit * self.instance_weight) / total_weight)

            # Get current usage and deficit
            usage_str = await backend.get(usage_key)
            usage = int(usage_str) if usage_str else 0

            deficit_str = await backend.get(deficit_key)
            deficit = int(deficit_str) if deficit_str else 0

            # Check global limit as well
            global_usage_str = await backend.get(global_usage_key)
            global_usage = int(global_usage_str) if global_usage_str else 0

            # Deficit round-robin
            quantum = fair_share + deficit

            if usage + cost <= quantum and global_usage + cost <= rate.limit:
                # Allowed - update counters
                await backend.increment_with_ttl(
                    usage_key, amount=cost, ttl=ttl_seconds
                )
                await backend.increment_with_ttl(
                    global_usage_key, amount=cost, ttl=ttl_seconds
                )

                # Update deficit
                new_deficit = quantum - (usage + cost)
                await backend.set(deficit_key, str(new_deficit), expire=ttl_seconds)
                return 0.0

            # Request denied. We calculate the wait period
            time_in_window = now % self.fairness_window_ms
            wait_ms = self.fairness_window_ms - time_in_window
            return max(wait_ms, 0.0)


@dataclass(frozen=True)
class GeographicDistributionStrategy:
    """
    Geographic rate limiting with region-specific limits.

    Distribute rate limits across geographic regions, useful for
    CDN-like scenarios or multi-region deployments.

    **Use case:** Global applications with region-specific capacity

    **How it works:**
    - Extract region from key or connection metadata
    - Apply region-specific limit multiplier
    - Optional spillover to other regions

    **Example:**
    ```python
    strategy = GeographicDistributionStrategy(
        region_multipliers={
            "us-east-1": 0.4,    # 40% of total capacity
            "us-west-2": 0.3,    # 30%
            "eu-west-1": 0.2,    # 20%
            "ap-southeast-1": 0.1,  # 10%
        },
        allow_spillover=True,  # Unused capacity → other regions
    )

    # Identifier: "region:{region}:user:{id}"
    throttle = HTTPThrottle(
        uid="global_api",
        rate="1000/minute",  # Total global capacity
        strategy=strategy,
    )
    ```

    **Use cases:**
    - CDN request routing
    - Multi-region deployments
    - Compliance with data residency
    - Cost optimization per region

    **Storage:**
    - `{key}:geo:{region}:{window}` - Per-region counters
    - `{key}:geo:spillover:{window}` - Spillover pool
    """

    region_multipliers: typing.Dict[str, float] = field(
        default_factory=lambda: {
            "default": 1.0,
        }
    )
    """Capacity multiplier per region (sums should = 1.0)"""

    allow_spillover: bool = True
    """Allow regions to use unused capacity from others"""

    default_region: str = "default"
    """Fallback region if not specified"""

    lock_config: LockConfig = field(
        default_factory=lambda: LockConfig(
            blocking=get_blocking_setting(),
            blocking_timeout=get_blocking_timeout(),
        )
    )
    """Configuration for backend locking during rate limit checks."""

    marker: str = "region:"
    """
    Marker used in keys to identify region segment.

    For example, with marker "region:", in key "...:region:us-east-1:...", "us-east-1" is the region.
    """

    def __post_init__(self) -> None:
        if not all(v >= 0.0 for v in self.region_multipliers.values()):
            raise ValueError("All region multipliers must be non-negative")

        if sum(self.region_multipliers.values()) > 1.0:
            raise ValueError("Sum of region multipliers must not exceed 1.0")

        if not self.marker.endswith(":"):
            object.__setattr__(self, "marker", self.marker + ":")

    def _get_region(self, key: str) -> str:
        """Extract region from key format: 'region:{region}:...'"""
        marker = self.marker
        if marker not in key:
            return self.default_region

        parts = key.split(marker)
        if len(parts) < 2:
            return self.default_region

        region_part = parts[1]
        region = region_part.split(":")[0]
        return region or self.default_region

    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0

        now = time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        region = self._get_region(str(key))
        multiplier = self.region_multipliers.get(
            region, self.region_multipliers.get("default", 1.0)
        )
        region_limit = int(rate.limit * multiplier)

        full_key = backend.get_key(str(key))
        region_key = f"{full_key}:geo:{region}:{current_window}"
        spillover_key = f"{full_key}:geo:spillover:{current_window}"
        ttl_seconds = max(int((2 * window_duration_ms) // 1000), 1)

        async with await backend.lock(f"lock:{region_key}", **self.lock_config):
            # Increment region counter
            region_count = await backend.increment_with_ttl(
                region_key, amount=cost, ttl=ttl_seconds
            )
            if region_count <= region_limit:
                # Within region limit
                return 0.0

            if self.allow_spillover:
                # Try spillover pool
                spillover_count = await backend.increment_with_ttl(
                    spillover_key, amount=cost, ttl=ttl_seconds
                )
                # Calculate total spillover capacity (sum of unused regional capacity)
                total_used = region_count  # Just this region for simplicity
                spillover_capacity = max(0, rate.limit - total_used)

                if spillover_count <= spillover_capacity:
                    return 0.0

            # Exceeded limits
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            return max(wait_ms, 0.0)
