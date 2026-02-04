"""
Advanced Rate Limiting Strategies for Special Use Cases
"""

import heapq
import time as pytime
import typing
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

from typing_extensions import TypedDict

from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import LockConfig, StrategyStat, Stringable, WaitPeriod
from traffik.utils import MsgPackDecodeError, dump_data, load_data, time

__all__ = [
    "TieredRate",
    "AdaptiveThrottle",
    "PriorityQueue",
    "QuotaWithRollover",
    "TimeOfDay",
    "CostBasedTokenBucket",
    "GCRA",
    "DistributedFairness",
    "GeographicDistribution",
    "TieredRateStrategy",
    "AdaptiveThrottleStrategy",
    "PriorityQueueStrategy",
    "QuotaWithRolloverStrategy",
    "TimeOfDayStrategy",
    "CostBasedTokenBucketStrategy",
    "GCRAStrategy",
    "DistributedFairnessStrategy",
    "GeographicDistributionStrategy",
    "TieredRateStatMetadata",
    "AdaptiveThrottleStatMetadata",
    "PriorityQueueStatMetadata",
    "QuotaWithRolloverStatMetadata",
    "TimeOfDayStatMetadata",
    "CostBasedTokenBucketStatMetadata",
    "GCRAStatMetadata",
    "DistributedFairnessStatMetadata",
    "GeographicDistributionStatMetadata",
]


class TieredRateStatMetadata(TypedDict):
    """
    Metadata for `TieredRateStrategy` statistics.

    The tiered rate strategy applies different rate limits based on
    user tiers (e.g., free, premium, enterprise).
    """

    strategy: typing.Literal["tiered_rate"]
    """Strategy identifier, always "tiered_rate"."""

    tier: str
    """The tier extracted from the key (e.g., "free", "premium", "enterprise")."""

    tier_multiplier: float
    """Rate multiplier applied for this tier."""

    effective_limit: int
    """Calculated effective limit (base_limit * tier_multiplier)."""

    current_count: int
    """Number of requests in the current window."""

    window_id: int
    """Identifier of the current time window."""

    window_start_ms: float
    """Start timestamp of the current window in milliseconds since epoch."""


class AdaptiveThrottleStatMetadata(TypedDict):
    """
    Metadata for `AdaptiveThrottleStrategy` statistics.

    The adaptive throttle strategy dynamically adjusts rate limits
    based on current load to protect backend services.
    """

    strategy: typing.Literal["adaptive_throttle"]
    """Strategy identifier, always "adaptive_throttle"."""

    effective_limit: float
    """Current effective rate limit (may be reduced under high load)."""

    current_count: int
    """Number of requests in the current window."""

    current_load: float
    """Current load percentage (current_count / effective_limit)."""

    load_threshold: float
    """Load threshold that triggers limit reduction."""

    window_id: int
    """Identifier of the current time window."""

    window_start_ms: float
    """Start timestamp of the current window in milliseconds since epoch."""


class PriorityQueueStatMetadata(TypedDict):
    """
    Metadata for `PriorityQueueStrategy` statistics.

    The priority queue strategy processes requests based on priority level,
    with higher priority requests taking precedence.
    """

    strategy: typing.Literal["priority_queue"]
    """Strategy identifier, always "priority_queue"."""

    priority: int
    """Priority level of the current request (1=LOW, 2=NORMAL, 3=HIGH, 4=CRITICAL)."""

    queue_size: int
    """Total number of entries in the priority queue."""

    total_cost_in_queue: float
    """Total cost of all entries in the queue."""

    higher_priority_cost: float
    """Total cost of entries with priority >= current request's priority."""


class QuotaWithRolloverStatMetadata(TypedDict):
    """
    Metadata for `QuotaWithRolloverStrategy` statistics.

    The quota with rollover strategy allows unused quota from previous
    periods to roll over, preventing "use it or lose it" wastage.
    """

    strategy: typing.Literal["quota_with_rollover"]
    """Strategy identifier, always "quota_with_rollover"."""

    base_limit: int
    """Base quota limit for the period."""

    rollover_amount: int
    """Amount of quota rolled over from the previous period."""

    effective_limit: int
    """Total effective limit (base_limit + rollover_amount)."""

    used: int
    """Amount of quota used in the current period."""

    period_id: int
    """Identifier of the current quota period."""

    period_start_ms: float
    """Start timestamp of the current period in milliseconds since epoch."""


class TimeOfDayStatMetadata(TypedDict):
    """
    Metadata for `TimeOfDayStrategy` statistics.

    The time of day strategy applies different rate limits based on
    the time of day (e.g., peak vs off-peak hours).
    """

    strategy: typing.Literal["time_of_day"]
    """Strategy identifier, always "time_of_day"."""

    hour_of_day: int
    """Current hour of day (0-23) in the configured timezone."""

    time_multiplier: float
    """Rate multiplier applied for the current time window."""

    effective_limit: int
    """Calculated effective limit (base_limit * time_multiplier)."""

    current_count: int
    """Number of requests in the current window."""

    window_id: int
    """Identifier of the current time window."""

    window_start_ms: float
    """Start timestamp of the current window in milliseconds since epoch."""


class CostBasedTokenBucketStatMetadata(TypedDict):
    """
    Metadata for `CostBasedTokenBucketStrategy` statistics.

    The cost-based token bucket strategy adjusts refill rate based on
    the average cost of recent requests.
    """

    strategy: typing.Literal["cost_based_token_bucket"]
    """Strategy identifier, always "cost_based_token_bucket"."""

    tokens: float
    """Current number of tokens in the bucket."""

    capacity: int
    """Maximum capacity of the bucket (burst size)."""

    average_cost: float
    """Average cost of requests in the tracking window."""

    cost_history_size: int
    """Number of requests tracked for cost averaging."""

    base_refill_rate_per_ms: float
    """Base token refill rate (before cost adjustment)."""

    effective_refill_rate_per_ms: float
    """Effective refill rate after cost-based adjustment."""

    last_refill_ms: float
    """Timestamp of the last token refill calculation in milliseconds since epoch."""


class GCRAStatMetadata(TypedDict):
    """
    Metadata for `GCRAStrategy` statistics.

    The GCRA (Generic Cell Rate Algorithm) provides perfectly smooth
    rate limiting with configurable burst tolerance.
    """

    strategy: typing.Literal["gcra"]
    """Strategy identifier, always "gcra"."""

    tat_ms: float
    """Theoretical Arrival Time - when the next request is expected."""

    emission_interval_ms: float
    """Time interval between allowed requests (window_duration / limit)."""

    burst_tolerance_ms: float
    """Configured burst tolerance in milliseconds."""

    conformant: bool
    """Whether the current state would allow a request (True) or not (False)."""


class DistributedFairnessStatMetadata(TypedDict):
    """
    Metadata for `DistributedFairnessStrategy` statistics.

    The distributed fairness strategy ensures fair rate limit distribution
    across multiple application instances using deficit round-robin.
    """

    strategy: typing.Literal["distributed_fairness"]
    """Strategy identifier, always "distributed_fairness"."""

    instance_id: str
    """Unique identifier of this application instance."""

    instance_weight: float
    """Weight assigned to this instance for weighted fair queuing."""

    fair_share: int
    """Calculated fair share of the global limit for this instance."""

    quantum: int
    """Current quantum (fair_share + deficit) for this instance."""

    instance_usage: int
    """Number of requests made by this instance in the current window."""

    global_usage: int
    """Total requests made by all instances in the current window."""

    deficit: int
    """Deficit counter for this instance (unused quota carried forward)."""

    active_instances: int
    """Number of active instances sharing the rate limit."""

    window_start_ms: float
    """Start timestamp of the current fairness window in milliseconds since epoch."""


class GeographicDistributionStatMetadata(TypedDict):
    """
    Metadata for `GeographicDistributionStrategy` statistics.

    The geographic distribution strategy distributes rate limits across
    geographic regions with optional spillover support.
    """

    strategy: typing.Literal["geographic_distribution"]
    """Strategy identifier, always "geographic_distribution"."""

    region: str
    """Region extracted from the key (e.g., "us-east-1", "eu-west-1")."""

    region_multiplier: float
    """Capacity multiplier for this region."""

    region_limit: int
    """Calculated rate limit for this region (global_limit * region_multiplier)."""

    region_count: int
    """Number of requests from this region in the current window."""

    spillover_count: int
    """Number of requests using spillover capacity."""

    allow_spillover: bool
    """Whether spillover to unused capacity from other regions is enabled."""

    window_id: int
    """Identifier of the current time window."""

    window_start_ms: float
    """Start timestamp of the current window in milliseconds since epoch."""


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
        """Extract tier from key format: '...:tier:{tier}:...'"""
        marker = self.marker
        idx = key.find(marker)
        if idx == -1:
            return self.default_tier

        start = idx + len(marker)
        end = key.find(":", start)
        tier = key[start:end] if end != -1 else key[start:]
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[TieredRateStatMetadata]:
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

        tier = self._get_tier(str(key))
        multiplier = self.tier_multipliers.get(tier, 1.0)
        effective_limit = int(rate.limit * multiplier)

        now = time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:tiered:{tier}:{current_window}"

        counter_str = await backend.get(counter_key)
        count = int(counter_str) if counter_str else 0

        hits_remaining = max(effective_limit - count, 0)
        if count > effective_limit:
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        window_start_ms = current_window * window_duration_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=TieredRateStatMetadata(
                strategy="tiered_rate",
                tier=tier,
                tier_multiplier=multiplier,
                effective_limit=effective_limit,
                current_count=count,
                window_id=current_window,
                window_start_ms=window_start_ms,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[AdaptiveThrottleStatMetadata]:
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
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:adaptive:{current_window}:counter"
        limit_key = f"{full_key}:adaptive:{current_window}:limit"

        counter_str, limit_str = await backend.multi_get(counter_key, limit_key)
        count = int(counter_str) if counter_str else 0
        effective_limit = float(limit_str) if limit_str else float(rate.limit)

        hits_remaining = max(effective_limit - count, 0.0)
        load = count / effective_limit if effective_limit > 0 else 0.0

        if count > effective_limit:
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        window_start_ms = current_window * window_duration_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=AdaptiveThrottleStatMetadata(
                strategy="adaptive_throttle",
                effective_limit=effective_limit,
                current_count=count,
                current_load=load,
                load_threshold=self.load_threshold,
                window_id=current_window,
                window_start_ms=window_start_ms,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
        """Extract priority from key format: '...:priority:{level}:...'"""
        marker = self.marker
        idx = key.find(marker)
        if idx == -1:
            return self.default_priority

        start = idx + len(marker)
        end = key.find(":", start)
        level_str = key[start:end] if end != -1 else key[start:]

        try:
            return Priority(int(level_str))
        except (ValueError, KeyError):
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

            # Filter expired, sum higher priority cost, find oldest high-priority timestamp in one pass
            cutoff = now - rate.expire
            filtered_queue = []
            higher_priority_cost = 0
            oldest_high_priority_ts = float("inf")

            for ts, pri, c in queue:
                if ts > cutoff:
                    filtered_queue.append([ts, pri, c])
                    if pri >= priority:
                        higher_priority_cost += c
                        if ts < oldest_high_priority_ts:
                            oldest_high_priority_ts = ts

            queue = filtered_queue

            # Check if we can accept this request
            if higher_priority_cost + cost > rate.limit:
                # Calculate wait time based on oldest high-priority request
                if oldest_high_priority_ts != float("inf"):
                    wait_ms = rate.expire - (now - oldest_high_priority_ts)
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
                    ),  # Sort by priority descending & timestamp descending order
                )

            # Save updated queue
            await backend.set(queue_key, dump_data(queue), expire=ttl_seconds)
            return 0.0

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[PriorityQueueStatMetadata]:
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
        priority = self._get_priority(str(key))
        full_key = backend.get_key(str(key))
        queue_key = f"{full_key}:priority:queue"

        queue_json = await backend.get(queue_key)
        if queue_json:
            try:
                queue = load_data(queue_json)
            except MsgPackDecodeError:
                queue = []
        else:
            queue = []

        # Filter expired, sum costs, find oldest high-priority timestamp in one pass
        cutoff = now - rate.expire
        filtered_queue = []
        higher_priority_cost = 0
        total_cost = 0
        oldest_high_priority_ts = float("inf")

        for ts, pri, c in queue:
            if ts > cutoff:
                filtered_queue.append([ts, pri, c])
                total_cost += c
                if pri >= priority:
                    higher_priority_cost += c
                    if ts < oldest_high_priority_ts:
                        oldest_high_priority_ts = ts

        queue = filtered_queue
        hits_remaining = max(rate.limit - higher_priority_cost, 0.0)

        if higher_priority_cost >= rate.limit:
            if oldest_high_priority_ts != float("inf"):
                wait_ms = rate.expire - (now - oldest_high_priority_ts)
                wait_ms = max(wait_ms, 0.0)
            else:
                wait_ms = rate.expire
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=PriorityQueueStatMetadata(
                strategy="priority_queue",
                priority=int(priority),
                queue_size=len(queue),
                total_cost_in_queue=total_cost,
                higher_priority_cost=higher_priority_cost,
            ),
        )


@dataclass(frozen=True)
class QuotaWithRolloverStrategy:
    """
    Quota-based rate limiting with rollover of unused quota.
    Implements fixed window quota with rollover.

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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
            # Get current usage and rollover
            used_str, rollover_str = await backend.multi_get(used_key, rollover_key)
            used = int(used_str) if used_str else 0

            # Use or calculate rollover for this period
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[QuotaWithRolloverStatMetadata]:
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
        window_duration_ms = rate.expire
        current_period = int(now // window_duration_ms)

        full_key = backend.get_key(str(key))
        used_key = f"{full_key}:quota:{current_period}:used"
        rollover_key = f"{full_key}:quota:{current_period}:rollover"

        used_str, rollover_str = await backend.multi_get(used_key, rollover_key)
        used = int(used_str) if used_str else 0
        rollover = int(rollover_str) if rollover_str else 0

        effective_limit = rate.limit + rollover
        hits_remaining = max(effective_limit - used, 0)

        if used > effective_limit:
            time_in_period = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_period
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        period_start_ms = current_period * window_duration_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=QuotaWithRolloverStatMetadata(
                strategy="quota_with_rollover",
                base_limit=rate.limit,
                rollover_amount=rollover,
                effective_limit=effective_limit,
                used=used,
                period_id=current_period,
                period_start_ms=period_start_ms,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[TimeOfDayStatMetadata]:
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

        # Use wall clock time for time-of-day calculations
        now = pytime.time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)

        multiplier = self._get_current_multiplier(now)
        effective_limit = int(rate.limit * multiplier)

        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:tod:{current_window}:counter"

        counter_str = await backend.get(counter_key)
        count = int(counter_str) if counter_str else 0

        hits_remaining = max(effective_limit - count, 0)
        if count > effective_limit:
            time_in_window = now % window_duration_ms
            wait_ms = window_duration_ms - time_in_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        # Calculate current hour for metadata
        hours_since_epoch = (now / 1000 / 3600) + self.timezone_offset
        hour_of_day = int(hours_since_epoch % 24)

        window_start_ms = current_window * window_duration_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=TimeOfDayStatMetadata(
                strategy="time_of_day",
                hour_of_day=hour_of_day,
                time_multiplier=multiplier,
                effective_limit=effective_limit,
                current_count=count,
                window_id=current_window,
                window_start_ms=window_start_ms,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
        cost_window = self.cost_window
        async with await backend.lock(f"lock:{state_key}", **self.lock_config):
            # Get bucket state and cost history
            state_json, history_json = await backend.multi_get(state_key, history_key)
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

            if history_json:
                try:
                    history = deque(load_data(history_json), maxlen=cost_window)
                except MsgPackDecodeError:
                    history = deque(maxlen=cost_window)
            else:
                history = deque(maxlen=cost_window)

            # Calculate average cost
            if history:
                avg_cost = sum(history) / len(history)
                # Adjust refill rate based on average cost
                # Higher average cost means a slower refill
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
                # Update history. The deque auto-trims to maxlen
                history.append(cost)

                # Save state and history
                new_state = {"tokens": tokens, "last_refill": now}
                await backend.multi_set(
                    {
                        state_key: dump_data(new_state),
                        history_key: dump_data(list(history)),
                    },
                    expire=ttl_seconds,
                )
                return 0.0

            # Calculate wait time for required tokens
            tokens_needed = cost - tokens
            wait_ms = tokens_needed / effective_refill_rate

            # Save current state
            new_state = {"tokens": tokens, "last_refill": now}
            await backend.set(state_key, dump_data(new_state), expire=ttl_seconds)
            return wait_ms

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[CostBasedTokenBucketStatMetadata]:
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
        base_refill_rate = rate.limit / refill_period_ms
        capacity = self.burst_size if self.burst_size is not None else rate.limit

        full_key = backend.get_key(str(key))
        state_key = f"{full_key}:costbucket:state"
        history_key = f"{full_key}:costbucket:history"

        state_json, history_json = await backend.multi_get(state_key, history_key)

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

        if history_json:
            try:
                history = load_data(history_json)
            except MsgPackDecodeError:
                history = []
        else:
            history = []

        # Calculate effective refill rate
        if history:
            avg_cost = sum(history) / len(history)
            cost_multiplier = max(self.min_refill_rate, 1.0 / avg_cost)
            effective_refill_rate = base_refill_rate * cost_multiplier
        else:
            avg_cost = 1.0
            effective_refill_rate = base_refill_rate

        # Refill tokens
        time_elapsed = now - last_refill
        tokens_to_add = effective_refill_rate * time_elapsed
        tokens = min(tokens + tokens_to_add, float(capacity))

        hits_remaining = max(tokens, 0.0)
        if tokens < 0:
            wait_ms = abs(tokens) / effective_refill_rate
        else:
            wait_ms = 0.0

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=CostBasedTokenBucketStatMetadata(
                strategy="cost_based_token_bucket",
                tokens=tokens,
                capacity=capacity,
                average_cost=avg_cost,
                cost_history_size=len(history),
                base_refill_rate_per_ms=base_refill_rate,
                effective_refill_rate_per_ms=effective_refill_rate,
                last_refill_ms=last_refill,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
                # Allowed request. Update TAT
                await backend.set(tat_key, str(new_tat), expire=ttl_seconds)
                return 0.0

            # Denied. Calculate wait time
            wait_ms = tat - now
            return max(wait_ms, 0.0)

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[GCRAStatMetadata]:
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
        emission_interval = rate.expire / rate.limit

        full_key = backend.get_key(str(key))
        tat_key = f"{full_key}:gcra:tat"

        tat_str = await backend.get(tat_key)
        tat = float(tat_str) if tat_str else now

        # Check if request would be conformant
        if now >= (tat - self.burst_tolerance_ms):
            # Would be allowed, calculate how many requests could be made
            conformant = True
            # Time ahead of schedule = how much buffer we have
            time_ahead = now - (tat - self.burst_tolerance_ms)
            hits_remaining = max(time_ahead / emission_interval, 0.0)
            wait_ms = 0.0
        else:
            # Would be denied
            conformant = False
            hits_remaining = 0.0
            wait_ms = tat - now
            wait_ms = max(wait_ms, 0.0)

        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=GCRAStatMetadata(
                strategy="gcra",
                tat_ms=tat,
                emission_interval_ms=emission_interval,
                burst_tolerance_ms=self.burst_tolerance_ms,
                conformant=conformant,
            ),
        )


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
    - Instance A only uses 200 and donates 100
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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]

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

            # Get current usage, deficit, and global usage
            usage_str, deficit_str, global_usage_str = await backend.multi_get(
                usage_key, deficit_key, global_usage_key
            )
            usage = int(usage_str) if usage_str else 0
            deficit = int(deficit_str) if deficit_str else 0
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[DistributedFairnessStatMetadata]:
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
        current_window = int(now // self.fairness_window_ms)

        full_key = backend.get_key(str(key))
        instances_key = f"{full_key}:dfq:instances"
        usage_key = f"{full_key}:dfq:usage:{self.instance_id}:{current_window}"
        deficit_key = f"{full_key}:dfq:deficit:{self.instance_id}:{current_window}"
        global_usage_key = f"{full_key}:dfq:global:{current_window}"

        (
            instances_json,
            usage_str,
            deficit_str,
            global_usage_str,
        ) = await backend.multi_get(
            instances_key, usage_key, deficit_key, global_usage_key
        )

        # Parse instances
        if instances_json:
            try:
                instances = load_data(instances_json)
            except MsgPackDecodeError:
                instances = {}
        else:
            instances = {}

        # Calculate fair share
        total_weight = (
            sum(data.get("weight", 1.0) for data in instances.values())
            or self.instance_weight
        )
        fair_share = int((rate.limit * self.instance_weight) / total_weight)

        usage = int(usage_str) if usage_str else 0
        deficit = int(deficit_str) if deficit_str else 0
        global_usage = int(global_usage_str) if global_usage_str else 0

        quantum = fair_share + deficit
        instance_remaining = max(quantum - usage, 0)
        global_remaining = max(rate.limit - global_usage, 0)
        hits_remaining = min(instance_remaining, global_remaining)

        if usage >= quantum or global_usage >= rate.limit:
            time_in_window = now % self.fairness_window_ms
            wait_ms = self.fairness_window_ms - time_in_window
            wait_ms = max(wait_ms, 0.0)
        else:
            wait_ms = 0.0

        window_start_ms = current_window * self.fairness_window_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=DistributedFairnessStatMetadata(
                strategy="distributed_fairness",
                instance_id=self.instance_id,
                instance_weight=self.instance_weight,
                fair_share=fair_share,
                quantum=quantum,
                instance_usage=usage,
                global_usage=global_usage,
                deficit=deficit,
                active_instances=len(instances),
                window_start_ms=window_start_ms,
            ),
        )


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

    lock_config: LockConfig = field(default_factory=LockConfig)  # type: ignore[arg-type]
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
        """Extract region from key format: '...:region:{region}:...'"""
        marker = self.marker
        idx = key.find(marker)
        if idx == -1:
            return self.default_region

        start = idx + len(marker)
        end = key.find(":", start)
        region = key[start:end] if end != -1 else key[start:]
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

    async def get_stat(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend
    ) -> StrategyStat[GeographicDistributionStatMetadata]:
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

        region_count_str, spillover_count_str = await backend.multi_get(
            region_key, spillover_key
        )
        region_count = int(region_count_str) if region_count_str else 0
        spillover_count = int(spillover_count_str) if spillover_count_str else 0

        # Calculate remaining hits
        region_remaining = max(region_limit - region_count, 0)
        if self.allow_spillover and region_remaining <= 0:
            spillover_capacity = max(0, rate.limit - region_count)
            spillover_remaining = max(spillover_capacity - spillover_count, 0)
            hits_remaining = spillover_remaining
        else:
            hits_remaining = region_remaining

        if region_count > region_limit:
            if not self.allow_spillover or spillover_count > (
                rate.limit - region_count
            ):
                time_in_window = now % window_duration_ms
                wait_ms = window_duration_ms - time_in_window
                wait_ms = max(wait_ms, 0.0)
            else:
                wait_ms = 0.0
        else:
            wait_ms = 0.0

        window_start_ms = current_window * window_duration_ms
        return StrategyStat(
            key=key,
            rate=rate,
            hits_remaining=hits_remaining,
            wait_ms=wait_ms,
            metadata=GeographicDistributionStatMetadata(
                strategy="geographic_distribution",
                region=region,
                region_multiplier=multiplier,
                region_limit=region_limit,
                region_count=region_count,
                spillover_count=spillover_count,
                allow_spillover=self.allow_spillover,
                window_id=current_window,
                window_start_ms=window_start_ms,
            ),
        )


TieredRate = TieredRateStrategy
AdaptiveThrottle = AdaptiveThrottleStrategy
TimeOfDay = TimeOfDayStrategy
PriorityQueue = PriorityQueueStrategy
QuotaWithRollover = QuotaWithRolloverStrategy
CostBasedTokenBucket = CostBasedTokenBucketStrategy
GCRA = GCRAStrategy
DistributedFairness = DistributedFairnessStrategy
GeographicDistribution = GeographicDistributionStrategy
