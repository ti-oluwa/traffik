"""TypedDict definitions for strategy statistics metadata.

This module provides typed metadata dictionaries for each rate limiting strategy,
enabling type-safe access to strategy-specific statistics and state information.
"""

import typing

from typing_extensions import TypedDict

__all__ = [
    # Regular strategies
    "FixedWindowStatMetadata",
    "SlidingWindowLogStatMetadata",
    "SlidingWindowCounterStatMetadata",
    "TokenBucketStatMetadata",
    "TokenBucketWithDebtStatMetadata",
    "LeakyBucketStatMetadata",
    "LeakyBucketWithQueueStatMetadata",
    # Custom strategies
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


# =============================================================================
# Regular Strategy Metadata Types
# =============================================================================


class FixedWindowStatMetadata(TypedDict):
    """Metadata for FixedWindowStrategy statistics.

    The fixed window strategy divides time into fixed intervals and counts
    requests within each window.
    """

    strategy: typing.Literal["fixed_window"]
    """Strategy identifier, always "fixed_window"."""

    window_start_ms: float
    """Start timestamp of the current window in milliseconds since epoch."""

    window_end_ms: float
    """End timestamp of the current window in milliseconds since epoch."""

    current_count: int
    """Number of requests (weighted by cost) in the current window."""


class SlidingWindowLogStatMetadata(TypedDict):
    """Metadata for SlidingWindowLogStrategy statistics.

    The sliding window log strategy maintains a log of request timestamps
    for precise rate limiting over a continuously sliding time window.
    """

    strategy: typing.Literal["sliding_window_log"]
    """Strategy identifier, always "sliding_window_log"."""

    window_start_ms: float
    """Start timestamp of the sliding window in milliseconds since epoch."""

    entry_count: int
    """Number of entries (requests) currently in the log within the window."""

    current_cost_sum: float
    """Total cost of all requests in the current sliding window."""

    oldest_entry_ms: typing.Optional[float]
    """Timestamp of the oldest entry in the log, or None if log is empty."""


class SlidingWindowCounterStatMetadata(TypedDict):
    """Metadata for SlidingWindowCounterStrategy statistics.

    The sliding window counter strategy uses weighted counters from
    current and previous windows to approximate a true sliding window.
    """

    strategy: typing.Literal["sliding_window_counter"]
    """Strategy identifier, always "sliding_window_counter"."""

    current_window_id: int
    """Identifier of the current time window (based on window duration)."""

    current_count: int
    """Request count in the current window."""

    previous_count: int
    """Request count in the previous window (used for weighted calculation)."""

    overlap_percentage: float
    """Percentage of the previous window that overlaps with the sliding window (0.0-1.0)."""

    weighted_count: float
    """Calculated weighted count: (previous_count * overlap_percentage) + current_count."""


class TokenBucketStatMetadata(TypedDict):
    """Metadata for TokenBucketStrategy statistics.

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
    """Metadata for TokenBucketWithDebtStrategy statistics.

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


class LeakyBucketStatMetadata(TypedDict):
    """Metadata for LeakyBucketStrategy statistics.

    The leaky bucket strategy models rate limiting as a bucket that leaks
    at a constant rate, enforcing smooth traffic output.
    """

    strategy: typing.Literal["leaky_bucket"]
    """Strategy identifier, always "leaky_bucket"."""

    bucket_level: float
    """Current fill level of the bucket (pending request cost)."""

    bucket_capacity: int
    """Maximum capacity of the bucket (rate limit)."""

    leak_rate_per_ms: float
    """Rate at which the bucket leaks (requests per millisecond)."""

    last_leak_ms: float
    """Timestamp of the last leak calculation in milliseconds since epoch."""


class LeakyBucketWithQueueStatMetadata(TypedDict):
    """Metadata for LeakyBucketWithQueueStrategy statistics.

    Enhanced leaky bucket that maintains a FIFO queue of requests
    for strict ordering guarantees.
    """

    strategy: typing.Literal["leaky_bucket_with_queue"]
    """Strategy identifier, always "leaky_bucket_with_queue"."""

    queue_size: int
    """Number of entries currently in the queue."""

    queue_cost: float
    """Total cost of all entries in the queue."""

    bucket_capacity: int
    """Maximum capacity of the bucket (rate limit)."""

    leak_rate_per_ms: float
    """Rate at which the bucket leaks (requests per millisecond)."""

    last_leak_ms: float
    """Timestamp of the last leak calculation in milliseconds since epoch."""


# =============================================================================
# Custom Strategy Metadata Types
# =============================================================================


class TieredRateStatMetadata(TypedDict):
    """Metadata for TieredRateStrategy statistics.

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
    """Metadata for AdaptiveThrottleStrategy statistics.

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
    """Metadata for PriorityQueueStrategy statistics.

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
    """Metadata for QuotaWithRolloverStrategy statistics.

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
    """Metadata for TimeOfDayStrategy statistics.

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
    """Metadata for CostBasedTokenBucketStrategy statistics.

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
    """Metadata for GCRAStrategy statistics.

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
    """Metadata for DistributedFairnessStrategy statistics.

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
    """Metadata for GeographicDistributionStrategy statistics.

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
