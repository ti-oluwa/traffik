"""Tests for custom/advanced rate limiting strategies."""

import asyncio
import datetime

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.custom import (
    AdaptiveThrottleStrategy,
    CostBasedTokenBucketStrategy,
    DistributedFairnessStrategy,
    GCRAStrategy,
    GeographicDistributionStrategy,
    PriorityQueueStrategy,
    QuotaWithRolloverStrategy,
    TieredRateStrategy,
    TimeOfDayStrategy,
)


class TestTieredRateStrategy:
    """Tests for `TieredRateStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_tier_extraction_from_key(self, backend: InMemoryBackend):
        """Test extracting tier from key."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy(
                tier_multipliers={"free": 1.0, "premium": 5.0}, default_tier="free"
            )
            rate = Rate.parse("10/s")

            # Free tier: 10 requests
            for _ in range(10):
                wait = await strategy("tier:free:user:123", rate, backend)
                assert wait == 0.0

            wait = await strategy("tier:free:user:123", rate, backend)
            assert wait > 0, "Free tier should be throttled after 10"

            # Premium tier: 50 requests (5x multiplier)
            for i in range(50):
                wait = await strategy("tier:premium:user:456", rate, backend)
                assert wait == 0.0, f"Premium request {i + 1} should be allowed"

            wait = await strategy("tier:premium:user:456", rate, backend)
            assert wait > 0, "Premium should be throttled after 50"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_default_tier_fallback(self, backend: InMemoryBackend):
        """Test falls back to default tier when tier not specified."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy(
                tier_multipliers={"free": 1.0, "premium": 5.0}, default_tier="free"
            )
            rate = Rate.parse("5/s")

            # Key without tier marker should use default
            for _ in range(5):
                wait = await strategy("user:789", rate, backend)
                assert wait == 0.0

            wait = await strategy("user:789", rate, backend)
            assert wait > 0

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_tier_isolation(self, backend: InMemoryBackend):
        """Test different tiers don't interfere."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy()
            rate = Rate.parse("10/s")

            # Exhaust premium tier
            for _ in range(50):
                await strategy("tier:premium:user:1", rate, backend)

            # Free tier should still work
            wait = await strategy("tier:free:user:2", rate, backend)
            assert wait == 0.0


class TestGCRAStrategy:
    """Tests for GCRA (Generic Cell Rate Algorithm) Strategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_perfectly_smooth_rate_limiting(self, backend: InMemoryBackend):
        """Test GCRA enforces smooth spacing with zero burst tolerance."""
        async with backend(close_on_exit=True):
            strategy = GCRAStrategy(burst_tolerance_ms=0)
            rate = Rate.parse("100/s")  # 1 request per 10ms
            key = "user:smooth"

            # First request should pass
            wait = await strategy(key, rate, backend, cost=1)
            assert wait == 0.0

            # Immediate second request should be throttled (needs 10ms spacing)
            wait = await strategy(key, rate, backend, cost=1)
            assert wait > 0
            assert wait <= 10, "Wait should be approximately emission interval"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_burst_tolerance(self, backend: InMemoryBackend):
        """Test GCRA with burst tolerance allows some burst."""
        async with backend(close_on_exit=True):
            strategy = GCRAStrategy(burst_tolerance_ms=100)  # Allow 100ms burst
            rate = Rate.parse("10/s")  # 1 per 100ms
            key = "user:burst"

            # With 100ms tolerance, should allow some immediate requests
            allowed = 0
            for _ in range(5):
                wait = await strategy(key, rate, backend, cost=1)
                if wait == 0.0:
                    allowed += 1

            assert allowed >= 2, "Should allow at least 2 requests with burst tolerance"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_cost_based_spacing(self, backend: InMemoryBackend):
        """Test GCRA correctly handles cost parameter."""
        async with backend(close_on_exit=True):
            strategy = GCRAStrategy(burst_tolerance_ms=0)
            rate = Rate.parse("100/s")  # Emission interval = 10ms
            key = "user:cost"

            # Request with cost=5 should reserve 50ms
            wait = await strategy(key, rate, backend, cost=5)
            assert wait == 0.0

            # Next request should need to wait ~50ms
            wait = await strategy(key, rate, backend, cost=1)
            assert wait >= 40, "Should wait for previous cost"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_gcra_wait_accuracy(self, backend: InMemoryBackend):
        """Test GCRA wait time calculations are accurate."""
        async with backend(close_on_exit=True):
            strategy = GCRAStrategy(burst_tolerance_ms=0)
            rate = Rate.parse("10/s")  # 100ms per request
            key = "user:wait"

            await strategy(key, rate, backend)
            wait = await strategy(key, rate, backend)

            # Should need to wait approximately 100ms
            assert 90 <= wait <= 110, f"Wait time {wait} should be ~100ms"


class TestAdaptiveThrottleStrategy:
    """Tests for `AdaptiveThrottleStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_reduces_limit_on_high_load(self, backend: InMemoryBackend):
        """Test adaptive strategy reduces limit when load is high."""
        async with backend(close_on_exit=True):
            strategy = AdaptiveThrottleStrategy(
                load_threshold=0.8,  # Trigger at 80% usage
                reduction_factor=0.5,  # Reduce to 50%
                recovery_rate=0.1,  # Recover 10% per window
                min_limit_ratio=0.3,  # Never go below 30%
            )
            rate = Rate.parse("100/s")
            key = "user:adaptive"

            # Fill to 90% of limit to trigger adaptation
            for i in range(90):
                wait = await strategy(key, rate, backend)
                # Initially all should pass
                if i < 80:  # Before hitting threshold
                    assert wait == 0.0, f"Request {i + 1} should pass before threshold"

            # Allow window to reset
            await asyncio.sleep(1.1)

            # New window should have adapted limit
            # The limit may have recovered slightly (recovery_rate=0.1 means +10 from reduced)
            # If previous effective limit was reduced to 50 (0.5 * 100),
            # new window starts at min(100, 50 + 10) = 60
            # But we need to test the actual behavior, not assume exact values

            # Make requests until throttled to find the effective limit
            requests_allowed = 0
            for i in range(100):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    requests_allowed += 1
                else:
                    break

            # The effective limit should be less than the original 100 due to adaptation
            assert requests_allowed < 100, (
                f"Adaptive strategy should reduce limit, but allowed {requests_allowed}/100"
            )
            # Should be at least the minimum (30% of 100 = 30)
            assert requests_allowed >= 30, (
                f"Should allow at least min_limit_ratio (30), but only allowed {requests_allowed}"
            )


class TestPriorityQueueStrategy:
    """Tests for `PriorityQueueStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_higher_priority_takes_precedence(self, backend: InMemoryBackend):
        """Test higher priority requests are allowed over lower priority."""
        async with backend(close_on_exit=True):
            strategy = PriorityQueueStrategy(max_queue_size=50)
            rate = Rate.parse("5/s")

            # Low priority key (priority level 1)
            key_low = "priority:1:user:123"
            # High priority key (priority level 10)
            key_high = "priority:10:user:123"

            # Fill with low priority requests (3 out of 5 allowed)
            for _ in range(3):
                wait = await strategy(key_low, rate, backend, cost=1)
                assert wait == 0.0, "Low priority requests should be allowed initially"

            # Make 2 more low priority requests to fill the limit
            for _ in range(2):
                wait = await strategy(key_low, rate, backend, cost=1)
                assert wait == 0.0, "Should reach the 5 request limit"

            # Low priority request should now be throttled
            wait_low = await strategy(key_low, rate, backend, cost=1)
            assert wait_low > 0, "Low priority should be throttled after limit reached"

            # High priority request should still be allowed (or queued with lower wait time)
            # Priority queuing allows high priority to bypass or get preferential treatment
            wait_high = await strategy(key_high, rate, backend, cost=1)
            # High priority either allowed immediately or has shorter wait than low priority
            assert wait_high <= wait_low, (
                "High priority should have equal or lower wait time than low priority"
            )


class TestQuotaWithRolloverStrategy:
    """Tests for `QuotaWithRolloverStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_quota_enforcement(self, backend: InMemoryBackend):
        """Test basic quota limiting without rollover."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5,
                max_rollover=50,
            )
            rate = Rate.parse("100/s")
            key = "user:quota"

            # Use entire quota (100 requests)
            for i in range(100):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed within quota"

            # Should be throttled after quota is exhausted
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after quota exhausted"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_rollover_unused_quota(self, backend: InMemoryBackend):
        """Test that unused quota rolls over to next period."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5,  # 50% of unused rolls over
                max_rollover=50,
            )
            rate = Rate.parse("100/s")
            key = "user:quota:rollover"

            # Use only 60 requests (40 unused)
            for i in range(60):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed"

            # Wait for new period/window
            await asyncio.sleep(1.1)

            # New period should have: 100 (base) + 20 (50% of 40 unused) = 120 quota
            # Make 120 requests - all should succeed
            for i in range(120):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed with rollover quota"
                )

            # 121st request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after rollover quota exhausted"

    @pytest.mark.anyio
    @pytest.mark.strategy
    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_max_rollover_limit(self, backend: InMemoryBackend):
        """Test that rollover respects `max_rollover` limit."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=1.0,  # Try to roll over 100% of unused
                max_rollover=30,  # But cap at 30
            )
            rate = Rate.parse("100/s")
            key = "user:quota:maxrollover"

            # Use only 10 requests (90 unused)
            for i in range(10):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0

            # Wait for new period
            await asyncio.sleep(1.1)

            # New period should have: 100 + 30 (capped at `max_rollover`, not 90)
            # Make 130 requests - all should succeed
            for i in range(130):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed (max rollover is 30)"
                )

            # 131st request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after hitting max rollover limit"

    @pytest.mark.anyio
    @pytest.mark.strategy
    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_no_rollover_when_quota_fully_used(self, backend: InMemoryBackend):
        """Test that no quota rolls over when fully consumed."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5, max_rollover=50
            )
            rate = Rate.parse("100/s")
            key = "user:quota:norollover"

            # Use entire quota (100 requests, 0 unused)
            for _ in range(100):
                await strategy(key, rate, backend)

            # Wait for new period
            await asyncio.sleep(1.05)

            # New period should have only base quota (100), no rollover
            for i in range(100):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed with base quota"

            # 101st request should be throttled (no rollover bonus)
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled with no rollover"


class TestTimeOfDayStrategy:
    """Tests for `TimeOfDayStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_current_time_window_multiplier(self, backend: InMemoryBackend):
        """Test that the current time window's multiplier is applied."""
        async with backend(close_on_exit=True):
            # Use UTC hour to match the strategy's time() function which returns UTC timestamp
            current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour

            # Set higher limit for current hour (avoid hour 23 to prevent wraparound issues)
            # Use a window that definitely contains the current hour
            if current_hour_utc == 23:
                # For hour 23, use window 22-24 instead of wrapping
                time_windows = [(22, 24, 2.0)]
            else:
                time_windows = [(current_hour_utc, current_hour_utc + 2, 2.0)]

            strategy = TimeOfDayStrategy(time_windows=time_windows, timezone_offset=0)
            rate = Rate.parse("10/s")
            key = "user:tod"

            # Should allow 20 requests (2x multiplier). We in the defined window.
            # Since base rate is 10/s, with 2.0 multiplier we get 20/s
            for i in range(20):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed with 2x multiplier"
                )

            # 21st request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after exceeding multiplied limit"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_multiple_time_windows(self, backend: InMemoryBackend):
        """Test strategy with multiple time windows."""
        async with backend(close_on_exit=True):
            current_hour = datetime.datetime.now(datetime.timezone.utc).hour

            # Define multiple windows around current time
            # Ensure current hour falls in the middle window
            if current_hour >= 2 and current_hour < 22:
                time_windows = [
                    (0, current_hour, 1.0),  # Before current: 1x
                    (current_hour, current_hour + 2, 3.0),  # Current: 3x
                    (current_hour + 2, 24, 1.0),  # After current: 1x
                ]
                expected_limit = 30  # 10 * 3.0
            else:
                # Edge case hours - use simpler window
                time_windows = [(0, 24, 2.0)]
                expected_limit = 20

            strategy = TimeOfDayStrategy(time_windows=time_windows, timezone_offset=0)
            rate = Rate.parse("10/s")
            key = "user:multiwindow"

            # Should allow requests according to current window's multiplier
            for i in range(expected_limit):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed in current window"
                )

            # Next request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after current window limit"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_default_multiplier_when_no_window_matches(
        self, backend: InMemoryBackend
    ):
        """Test that default 1.0 multiplier is used when no window matches."""
        async with backend(close_on_exit=True):
            current_hour = datetime.datetime.now(datetime.timezone.utc).hour

            # Define windows that don't include current hour
            if current_hour < 12:
                # Current is morning, define only afternoon/evening windows
                time_windows = [(12, 18, 2.0), (18, 24, 1.5)]
            else:
                # Current is afternoon/evening, define only morning windows
                time_windows = [(0, 6, 2.0), (6, 12, 1.5)]

            strategy = TimeOfDayStrategy(time_windows=time_windows, timezone_offset=0)
            rate = Rate.parse("10/s")
            key = "user:default"

            # Should use default 1.0 multiplier (10 requests)
            for i in range(10):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed with default 1.0x"
                )

            # 11th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled with default multiplier"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_timezone_offset_adjustment(self, backend: InMemoryBackend):
        """Test that timezone_offset correctly adjusts the hour calculation."""
        async with backend(close_on_exit=True):
            current_hour_utc = datetime.datetime.now(datetime.timezone.utc).hour

            # With +5 hour offset, the effective hour shifts forward
            # If current UTC is 10, effective becomes 15
            timezone_offset = 5
            effective_hour = (current_hour_utc + timezone_offset) % 24

            # Define window around the effective hour
            if effective_hour >= 2 and effective_hour < 22:
                time_windows = [(effective_hour, effective_hour + 2, 2.5)]
            else:
                # For edge hours, use a safe window
                time_windows = [(0, 24, 2.5)]

            strategy = TimeOfDayStrategy(
                time_windows=time_windows, timezone_offset=timezone_offset
            )
            rate = Rate.parse("10/s")
            key = "user:timezone"

            # Should apply multiplier based on adjusted timezone
            # Expected: 10 * 2.5 = 25 requests
            for i in range(25):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed with timezone adjustment"
                )

            # 26th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after timezone-adjusted limit"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_different_multipliers_for_different_periods(
        self, backend: InMemoryBackend
    ):
        """Test that different time periods can have different multipliers."""
        async with backend(close_on_exit=True):
            current_hour = datetime.datetime.now(datetime.timezone.utc).hour

            # Create distinct windows with different multipliers
            # Use a configuration that definitely includes current hour
            if current_hour < 8:
                # Night hours - use high multiplier
                time_windows = [(0, 8, 3.0), (8, 16, 1.0), (16, 24, 2.0)]
                expected_multiplier = 3.0
            elif current_hour < 16:
                # Day hours - use low multiplier
                time_windows = [(0, 8, 3.0), (8, 16, 1.0), (16, 24, 2.0)]
                expected_multiplier = 1.0
            else:
                # Evening hours - use medium multiplier
                time_windows = [(0, 8, 3.0), (8, 16, 1.0), (16, 24, 2.0)]
                expected_multiplier = 2.0

            strategy = TimeOfDayStrategy(time_windows=time_windows, timezone_offset=0)
            rate = Rate.parse("10/s")
            key = "user:periods"

            expected_limit = int(10 * expected_multiplier)

            # Should allow requests according to current period's multiplier
            for i in range(expected_limit):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed with {expected_multiplier}x multiplier"
                )

            # Next request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, (
                f"Should be throttled after {expected_limit} requests "
                f"({expected_multiplier}x multiplier)"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_window_isolation_across_time_periods(self, backend: InMemoryBackend):
        """Test that each time window has its own counter."""
        async with backend(close_on_exit=True):
            # This test verifies windows are isolated
            current_hour = datetime.datetime.now(datetime.timezone.utc).hour

            # Create windows where we know which one we're in
            if current_hour < 12:
                # Morning: 2.0x multiplier (20 requests allowed)
                time_windows = [(0, 12, 2.0), (12, 24, 1.0)]
                expected_limit = 20
            else:
                # Afternoon/Evening: 1.0x multiplier (10 requests allowed)
                time_windows = [(0, 12, 2.0), (12, 24, 1.0)]
                expected_limit = 10

            strategy = TimeOfDayStrategy(time_windows=time_windows, timezone_offset=0)
            rate = Rate.parse("10/s")
            key = "user:isolation"

            # Make requests up to the current window's limit
            for i in range(expected_limit):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed in current window"
                )

            # Next request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, f"Should be throttled after {expected_limit} requests"


class TestCostBasedTokenBucketStrategy:
    """Tests for `CostBasedTokenBucketStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_burst_capacity_with_uniform_costs(self, backend: InMemoryBackend):
        """Test burst capacity with uniform cost requests."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(
                burst_size=50,  # 50 token capacity
                cost_window=10,
            )
            rate = Rate.parse("10/s")  # 10 tokens per second refill
            key = "user:uniform"

            # With burst=50 and cost=1, should allow 50 immediate requests
            for i in range(50):
                wait = await strategy(key, rate, backend, cost=1)
                assert wait == 0.0, (
                    f"Request {i + 1} should be allowed from burst capacity"
                )

            # 51st request should be throttled (bucket exhausted)
            wait = await strategy(key, rate, backend, cost=1)
            assert wait > 0, "Should be throttled after burst capacity exhausted"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_high_cost_consumes_more_tokens(self, backend: InMemoryBackend):
        """Test that high-cost requests consume proportionally more tokens."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(burst_size=100, cost_window=10)
            rate = Rate.parse("100/s")
            key = "user:highcost"

            # Make 5 requests with cost=20 each (total 100 tokens)
            for i in range(5):
                wait = await strategy(key, rate, backend, cost=20)
                assert wait == 0.0, f"High-cost request {i + 1} should be allowed"

            # Bucket should be exhausted (100 tokens consumed)
            wait = await strategy(key, rate, backend, cost=1)
            assert wait > 0, (
                "Should be throttled after bucket exhausted by high-cost requests"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_refill_rate_adjustment_based_on_average_cost(
        self, backend: InMemoryBackend
    ):
        """Test that refill rate adjusts based on average request cost."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(
                burst_size=100,
                cost_window=5,  # Track last 5 requests
                min_refill_rate=0.5,
            )
            rate = Rate.parse("100/s")
            key = "user:adaptive"

            # Build history with high average cost (avg = 20)
            for _ in range(5):
                await strategy(key, rate, backend, cost=20)

            # Exhaust remaining tokens (100 - 100 = 0)
            # Should be throttled now
            wait = await strategy(key, rate, backend, cost=1)
            assert wait > 0, "Should be throttled after exhausting bucket"

            # Wait for refill
            await asyncio.sleep(0.2)

            # With high avg_cost=20, refill rate is reduced by cost_multiplier
            # cost_multiplier = max(0.5, 1.0/20) = max(0.5, 0.05) = 0.5
            # effective_refill_rate = 100/s * 0.5 = 50/s
            # In 0.2s, should refill ~10 tokens (50 * 0.2 = 10)

            # Try to make 15 requests - should only allow ~10
            allowed = 0
            for _ in range(15):
                wait = await strategy(key, rate, backend, cost=1)
                if wait == 0.0:
                    allowed += 1
                else:
                    break

            # Should allow fewer requests due to reduced refill rate
            assert allowed <= 12, (
                f"With reduced refill rate, should allow ~10 requests, got {allowed}"
            )
            assert allowed >= 5, f"Should still refill some tokens, got {allowed}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_cost_history_window_limits_tracking(self, backend: InMemoryBackend):
        """Test that cost history respects the `cost_window` limit."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(
                burst_size=50,
                cost_window=3,  # Only track last 3 requests
            )
            rate = Rate.parse("100/s")
            key = "user:window"

            # First 3 requests with cost=1 (total: 3 tokens, 47 remaining)
            for _ in range(3):
                await strategy(key, rate, backend, cost=1)

            # Next 3 requests with cost=10 (total: 30 tokens, 17 remaining)
            # These should replace the first 3 in history
            for _ in range(3):
                await strategy(key, rate, backend, cost=10)

            # Cost history now has [10, 10, 10], avg_cost = 10
            # With 17 tokens remaining, a cost=20 request should be throttled
            wait = await strategy(key, rate, backend, cost=20)
            assert wait > 0, (
                "Should be throttled with insufficient tokens for high cost"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_token_bucket_refills_over_time(self, backend: InMemoryBackend):
        """Test that tokens refill over time."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(burst_size=20, cost_window=5)
            rate = Rate.parse("100/s")  # 100 tokens per second = 0.1 tokens per ms
            key = "user:refill"

            # Exhaust the bucket (20 tokens) in twos (instead of ones) so it slows the refill
            # rate and we avoid boundary issues that is peculiar to token bucket refill timing.
            for _ in range(10):
                await strategy(key, rate, backend, cost=2)

            # Should be throttled immediately
            wait = await strategy(key, rate, backend, cost=1)
            assert wait > 0, "Should be throttled after exhausting bucket"

            # Wait for refill (0.1 seconds should add max 10 tokens)
            await asyncio.sleep(0.1)

            # Should be able to make ~10 more requests
            allowed = 0
            for _ in range(15):  # Try 15 to see how many succeed
                wait = await strategy(key, rate, backend, cost=1)
                if wait == 0.0:
                    allowed += 1
                else:
                    break

            # Should have allowed some requests from refilled tokens
            assert allowed >= 4, (
                f"Should allow at least 4 requests after refill, got {allowed}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_min_refill_rate_prevents_starvation(self, backend: InMemoryBackend):
        """Test that `min_refill_rate` prevents complete starvation."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(
                burst_size=100,
                cost_window=5,
                min_refill_rate=0.5,  # Never go below 50% of base refill rate
            )
            rate = Rate.parse("100/s")
            key = "user:starve"

            # Build history with very high costs to trigger low refill rate
            for _ in range(5):
                await strategy(key, rate, backend, cost=100)

            # Even with high average cost, refill rate should be at least 50% of base
            # This prevents complete starvation
            # Wait a bit and verify we can still make requests eventually
            await asyncio.sleep(0.5)

            # Should eventually allow a request due to `min_refill_rate`
            wait = await strategy(key, rate, backend, cost=10)
            # Verify it works without completely blocking
            assert wait == 0


class TestDistributedFairnessStrategy:
    """Tests for `DistributedFairnessStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_equal_weight_fair_sharing(self, backend: InMemoryBackend):
        """Test instances with equal weights get equal shares."""
        async with backend(close_on_exit=True):
            strategy1 = DistributedFairnessStrategy(
                instance_id="instance1", instance_weight=1.0
            )
            strategy2 = DistributedFairnessStrategy(
                instance_id="instance2", instance_weight=1.0
            )
            rate = Rate.parse("100/s")  # 100 total limit
            key = "global:limit"

            # With 2 equal-weight instances, each gets 50 requests (fair share)
            # Test that both instances can make requests
            requests_inst1 = 0
            requests_inst2 = 0

            # Alternate requests to test fair sharing
            for _ in range(50):
                wait1 = await strategy1(key, rate, backend)
                if wait1 == 0.0:
                    requests_inst1 += 1

                wait2 = await strategy2(key, rate, backend)
                if wait2 == 0.0:
                    requests_inst2 += 1

            # Both instances should have been allowed requests
            assert requests_inst1 >= 25, (
                f"Instance1 should get at least 25 requests, got {requests_inst1}"
            )
            assert requests_inst2 >= 25, (
                f"Instance2 should get at least 25 requests, got {requests_inst2}"
            )

            # Total should not exceed global limit
            assert requests_inst1 + requests_inst2 <= 100, (
                "Total requests should not exceed global limit"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_weighted_proportional_sharing(self, backend: InMemoryBackend):
        """Test weighted instances get proportional shares."""
        async with backend(close_on_exit=True):
            # High weight instance (weight=3)
            high_weight = DistributedFairnessStrategy(
                instance_id="heavy", instance_weight=3.0
            )
            # Low weight instance (weight=1)
            low_weight = DistributedFairnessStrategy(
                instance_id="light", instance_weight=1.0
            )
            rate = Rate.parse("80/s")  # 80 total limit for cleaner division
            key = "weighted:limit"

            # Total weight = 3 + 1 = 4
            # Heavy gets: 80 * (3/4) = 60 requests (fair share)
            # Light gets: 80 * (1/4) = 20 requests (fair share)

            # Test each instance independently to avoid deficit interference
            # First, verify heavy instance can make more requests
            heavy_requests = 0
            for _ in range(65):
                wait = await high_weight(key, rate, backend, cost=1)
                if wait == 0.0:
                    heavy_requests += 1
                else:
                    break

            # Heavy should be allowed significantly more than 20 (light's fair share)
            assert heavy_requests >= 40, (
                f"Heavy weight instance should get at least 40 requests, "
                f"got {heavy_requests}"
            )

            # Now test with fresh key for light instance
            key2 = "weighted:limit2"
            light_requests = 0
            for _ in range(30):
                wait = await low_weight(key2, rate, backend, cost=1)
                if wait == 0.0:
                    light_requests += 1
                else:
                    break

            # Light should be throttled before heavy's count
            assert light_requests < heavy_requests, (
                f"Light weight (1.0) should get fewer requests than heavy (3.0), "
                f"got light={light_requests}, heavy={heavy_requests}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_global_limit_enforcement(self, backend: InMemoryBackend):
        """Test that global limit is enforced across all instances."""
        async with backend(close_on_exit=True):
            strategy1 = DistributedFairnessStrategy(
                instance_id="inst1", instance_weight=1.0
            )
            strategy2 = DistributedFairnessStrategy(
                instance_id="inst2", instance_weight=1.0
            )
            rate = Rate.parse("60/s")  # 60 total, 30 each
            key = "global:enforcement"

            # Each instance can use up to 30, but global limit is 60
            # Use 30 from instance1
            for _ in range(30):
                await strategy1(key, rate, backend)

            # Use 30 from instance2 (now at global limit of 60)
            for _ in range(30):
                await strategy2(key, rate, backend)

            # Both instances should now be throttled (global limit reached)
            wait1 = await strategy1(key, rate, backend)
            wait2 = await strategy2(key, rate, backend)

            assert wait1 > 0, "Instance1 should be throttled at global limit"
            assert wait2 > 0, "Instance2 should be throttled at global limit"


class TestGeographicDistributionStrategy:
    """Tests for `GeographicDistributionStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_region_extraction(self, backend: InMemoryBackend):
        """Test extracts region from key."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={
                    "us-east-1": 0.5,  # 50% of capacity
                    "eu-west-1": 0.3,  # 30% of capacity
                },
                default_region="default",
                allow_spillover=False,  # Disable spillover for strict region isolation
            )
            rate = Rate.parse("100/s")

            # US East should get 50 requests
            for i in range(50):
                wait = await strategy("region:us-east-1:user:123", rate, backend)
                assert wait == 0.0, f"US East request {i + 1} should be allowed"

            wait = await strategy("region:us-east-1:user:123", rate, backend)
            assert wait > 0, "US East should be throttled after 50"

            # EU West should still have capacity
            wait = await strategy("region:eu-west-1:user:456", rate, backend)
            assert wait == 0.0

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_spillover_uses_unused_capacity(self, backend: InMemoryBackend):
        """Test spillover allows using unused capacity from other regions."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={
                    "us-east-1": 0.6,  # 60 requests
                    "eu-west-1": 0.4,  # 40 requests (unused)
                },
                allow_spillover=True,
            )
            rate = Rate.parse("100/s")
            key_us = "region:us-east-1:user:123"

            # Use full US capacity (60 requests)
            for i in range(60):
                wait = await strategy(key_us, rate, backend)
                assert wait == 0.0, (
                    f"US request {i + 1} should be allowed within region limit"
                )

            # With spillover enabled, should be able to use some unused capacity
            # Since EU region (40 requests) is unused, spillover pool has capacity
            # Try a few more requests - at least some should succeed with spillover
            spillover_allowed = 0
            for _ in range(10):
                wait = await strategy(key_us, rate, backend)
                if wait == 0.0:
                    spillover_allowed += 1

            # Should allow at least 1 request via spillover (exact amount depends on implementation)
            assert spillover_allowed >= 1, (
                f"With spillover enabled, should allow some requests beyond regional limit, "
                f"got {spillover_allowed}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_no_spillover_strict_isolation(self, backend: InMemoryBackend):
        """Test that disabling spillover enforces strict regional limits."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={
                    "us-east-1": 0.6,  # 60 requests
                    "eu-west-1": 0.4,  # 40 requests
                },
                allow_spillover=False,  # Strict isolation
            )
            rate = Rate.parse("100/s")
            key_us = "region:us-east-1:user:123"

            # Use full US capacity (60 requests)
            for i in range(60):
                wait = await strategy(key_us, rate, backend)
                assert wait == 0.0, f"US request {i + 1} should be allowed"

            # With spillover disabled, 61st request should be throttled
            # even though EU region capacity is unused
            wait = await strategy(key_us, rate, backend)
            assert wait > 0, (
                "Without spillover, should be throttled at regional limit "
                "even with unused capacity in other regions"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_default_region_fallback(self, backend: InMemoryBackend):
        """Test uses default region when not specified."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={"default": 1.0}
            )
            rate = Rate.parse("10/s")

            # Key without region marker
            for _ in range(10):
                wait = await strategy("user:123", rate, backend)
                assert wait == 0.0

            wait = await strategy("user:123", rate, backend)
            assert wait > 0


@pytest.mark.anyio
@pytest.mark.strategy
async def test_all_strategies_handle_unlimited_rate(backend: InMemoryBackend):
    """Test all strategies handle unlimited rates correctly."""
    async with backend(close_on_exit=True):
        rate = Rate(limit=0, seconds=0)  # Unlimited
        key = "user:unlimited"

        strategies = [
            TieredRateStrategy(),
            GCRAStrategy(),
            AdaptiveThrottleStrategy(),
            PriorityQueueStrategy(),
            QuotaWithRolloverStrategy(),
            TimeOfDayStrategy(),
            CostBasedTokenBucketStrategy(),
            DistributedFairnessStrategy(instance_id="test"),
            GeographicDistributionStrategy(),
        ]

        for strategy in strategies:
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"{strategy.__class__.__name__} should allow unlimited"
                )


@pytest.mark.anyio
@pytest.mark.strategy
async def test_all_strategies_handle_concurrent_requests(backend: InMemoryBackend):
    """Test all strategies work under concurrent load."""
    async with backend(close_on_exit=True):
        rate = Rate.parse("50/s")
        key = "user:concurrent"

        strategies = [
            TieredRateStrategy(),
            GCRAStrategy(burst_tolerance_ms=1000),
            QuotaWithRolloverStrategy(),
            CostBasedTokenBucketStrategy(),
        ]

        for strategy in strategies:
            # Make concurrent requests
            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(10)]
            )
            # Just verify no crashes
            assert all(isinstance(r, float) for r in results)


class TestCustomStrategiesGetStat:
    """Tests for get_stat methods on custom strategies."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_tiered_rate_strategy_get_stat(self, backend: InMemoryBackend):
        """Test TieredRateStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy(
                tier_multipliers={"free": 1.0, "premium": 5.0},
                default_tier="free",
            )
            rate = Rate.parse("10/s")
            key = "tier:premium:user:stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.rate == rate
            assert stat.hits_remaining == 50  # 10 * 5.0 multiplier
            assert stat.wait_ms == 0.0
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "tiered_rate"
            assert stat.metadata["tier"] == "premium"
            assert stat.metadata["tier_multiplier"] == 5.0
            assert stat.metadata["effective_limit"] == 50

            # Make some requests
            for _ in range(20):
                await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.hits_remaining == 30
            assert stat.metadata["current_count"] == 20  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_adaptive_throttle_strategy_get_stat(self, backend: InMemoryBackend):
        """Test AdaptiveThrottleStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = AdaptiveThrottleStrategy(load_threshold=0.8)
            rate = Rate.parse("20/s")
            key = "user:adaptive_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.hits_remaining >= 0
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "adaptive_throttle"
            assert stat.metadata["load_threshold"] == 0.8

            # Make requests to increase load
            for _ in range(10):
                await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["current_count"] == 10  # type: ignore
            assert stat.metadata["current_load"] > 0  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_priority_queue_strategy_get_stat(self, backend: InMemoryBackend):
        """Test PriorityQueueStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = PriorityQueueStrategy()
            rate = Rate.parse("10/s")
            key = "user:priority_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "priority_queue"
            assert stat.metadata["queue_size"] == 0
            assert stat.metadata["total_cost_in_queue"] == 0.0

            # Add requests with different priorities
            await strategy(key, rate, backend, cost=2)
            await strategy(key, rate, backend, cost=3)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["queue_size"] >= 0  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_quota_with_rollover_strategy_get_stat(
        self, backend: InMemoryBackend
    ):
        """Test QuotaWithRolloverStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5,
                max_rollover=500,
            )
            rate = Rate.parse("20/s")
            key = "user:quota_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.hits_remaining >= 0
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "quota_with_rollover"
            assert stat.metadata["base_limit"] == 20

            # Make some requests
            for _ in range(5):
                await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["used"] == 5  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_time_of_day_strategy_get_stat(self, backend: InMemoryBackend):
        """Test TimeOfDayStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = TimeOfDayStrategy(
                time_windows=[
                    (0, 6, 0.5),  # Night
                    (6, 12, 1.0),  # Morning
                    (12, 18, 1.5),  # Afternoon
                    (18, 24, 1.0),  # Evening
                ],
                timezone_offset=0,
            )
            rate = Rate.parse("10/s")
            key = "user:tod_stat"

            # Get stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "time_of_day"
            assert 0 <= stat.metadata["hour_of_day"] <= 23
            assert stat.metadata["time_multiplier"] > 0
            assert stat.metadata["effective_limit"] > 0

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_cost_based_token_bucket_strategy_get_stat(
        self, backend: InMemoryBackend
    ):
        """Test CostBasedTokenBucketStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(burst_size=15)
            rate = Rate.parse("10/s")
            key = "user:costbucket_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.hits_remaining >= 0
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "cost_based_token_bucket"
            assert stat.metadata["capacity"] == 15

            # Make requests with varying costs
            await strategy(key, rate, backend, cost=3)
            await strategy(key, rate, backend, cost=5)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["cost_history_size"] >= 0  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_gcra_strategy_get_stat(self, backend: InMemoryBackend):
        """Test GCRAStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = GCRAStrategy(burst_tolerance_ms=100)
            rate = Rate.parse("10/s")  # emission interval = 100ms
            key = "user:gcra_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "gcra"
            assert stat.metadata["emission_interval_ms"] == 100.0  # 1000ms / 10
            assert stat.metadata["burst_tolerance_ms"] == 100
            assert stat.metadata["conformant"] is True

            # Make a request
            await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["tat_ms"] > 0  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_distributed_fairness_strategy_get_stat(
        self, backend: InMemoryBackend
    ):
        """Test DistributedFairnessStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = DistributedFairnessStrategy(
                instance_id="test-instance",
                instance_weight=1.0,
            )
            rate = Rate.parse("100/s")
            key = "user:fairness_stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "distributed_fairness"
            assert stat.metadata["instance_id"] == "test-instance"
            assert stat.metadata["instance_weight"] == 1.0

            # Make some requests
            for _ in range(10):
                await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["instance_usage"] == 10  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_geographic_distribution_strategy_get_stat(
        self, backend: InMemoryBackend
    ):
        """Test GeographicDistributionStrategy get_stat method."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={
                    "us-east": 0.6,
                    "eu-west": 0.4,
                },
                default_region="us-east",
            )
            rate = Rate.parse("50/s")
            key = "region:eu-west:user:stat"

            # Initial stat
            stat = await strategy.get_stat(key, rate, backend)
            assert stat.key == key
            assert stat.metadata is not None
            assert stat.metadata["strategy"] == "geographic_distribution"
            assert stat.metadata["region"] == "eu-west"
            assert stat.metadata["region_multiplier"] == 0.4
            assert stat.metadata["region_limit"] == 20  # 50 * 0.4

            # Make some requests
            for _ in range(10):
                await strategy(key, rate, backend)

            stat = await strategy.get_stat(key, rate, backend)
            assert stat.metadata["region_count"] == 10  # type: ignore

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_all_custom_strategies_get_stat_unlimited(
        self, backend: InMemoryBackend
    ):
        """Test all custom strategies handle unlimited rate in get_stat."""
        async with backend(close_on_exit=True):
            rate = Rate(limit=0, seconds=0)  # Unlimited
            key = "user:stat_unlimited"

            strategies = [
                TieredRateStrategy(),
                AdaptiveThrottleStrategy(),
                PriorityQueueStrategy(),
                QuotaWithRolloverStrategy(),
                TimeOfDayStrategy(),
                CostBasedTokenBucketStrategy(),
                GCRAStrategy(),
                DistributedFairnessStrategy(instance_id="test"),
                GeographicDistributionStrategy(),
            ]

            for strategy in strategies:
                stat = await strategy.get_stat(key, rate, backend)
                assert stat.hits_remaining == float("inf"), (
                    f"{strategy.__class__.__name__} should return inf for unlimited"
                )
                assert stat.wait_ms == 0.0, (
                    f"{strategy.__class__.__name__} should have no wait for unlimited"
                )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_all_custom_strategies_get_stat_has_required_fields(
        self, backend: InMemoryBackend
    ):
        """Test all custom strategies return stats with required fields."""
        async with backend(close_on_exit=True):
            rate = Rate.parse("100/s")
            key = "user:stat_fields"

            strategies = [
                TieredRateStrategy(),
                AdaptiveThrottleStrategy(),
                PriorityQueueStrategy(),
                QuotaWithRolloverStrategy(),
                TimeOfDayStrategy(),
                CostBasedTokenBucketStrategy(),
                GCRAStrategy(),
                DistributedFairnessStrategy(instance_id="test"),
                GeographicDistributionStrategy(),
            ]

            for strategy in strategies:
                stat = await strategy.get_stat(key, rate, backend)
                name = strategy.__class__.__name__

                # Check required fields
                assert hasattr(stat, "key"), f"{name} stat missing 'key'"
                assert hasattr(stat, "rate"), f"{name} stat missing 'rate'"
                assert hasattr(stat, "hits_remaining"), (
                    f"{name} stat missing 'hits_remaining'"
                )
                assert hasattr(stat, "wait_ms"), f"{name} stat missing 'wait_ms'"
                assert hasattr(stat, "metadata"), f"{name} stat missing 'metadata'"

                # Check types
                assert isinstance(stat.hits_remaining, (int, float)), (
                    f"{name} hits_remaining should be numeric"
                )
                assert isinstance(stat.wait_ms, (int, float)), (
                    f"{name} wait_ms should be numeric"
                )
                assert stat.metadata is not None, f"{name} metadata should not be None"
                assert "strategy" in stat.metadata, (
                    f"{name} metadata should have 'strategy' key"
                )
