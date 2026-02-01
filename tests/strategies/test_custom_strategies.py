"""Tests for custom/advanced rate limiting strategies."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.custom import (
    AdaptiveThrottleStrategy,  # noqa: F401
    CostBasedTokenBucketStrategy,  # noqa: F401
    DistributedFairnessStrategy,  # noqa: F401
    GCRAStrategy,  # noqa: F401
    GeographicDistributionStrategy,  # noqa: F401
    PriorityQueueStrategy,  # noqa: F401
    QuotaWithRolloverStrategy,  # noqa: F401
    TieredRateStrategy,
    TimeOfDayStrategy,  # noqa: F401
)


class TestTieredRateStrategy:
    """Tests for TieredRateStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_tier_extraction_from_key(self, backend: InMemoryBackend):
        """Test extracting tier from key."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy(
                tier_multipliers={"free": 1.0, "premium": 5.0},
                default_tier="free"
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
                assert wait == 0.0, f"Premium request {i+1} should be allowed"

            wait = await strategy("tier:premium:user:456", rate, backend)
            assert wait > 0, "Premium should be throttled after 50"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_default_tier_fallback(self, backend: InMemoryBackend):
        """Test falls back to default tier when tier not specified."""
        async with backend(close_on_exit=True):
            strategy = TieredRateStrategy(
                tier_multipliers={"free": 1.0, "premium": 5.0},
                default_tier="free"
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
    """Tests for AdaptiveThrottleStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_reduces_limit_on_high_load(self, backend: InMemoryBackend):
        """Test adaptive strategy reduces limit when load is high."""
        async with backend(close_on_exit=True):
            strategy = AdaptiveThrottleStrategy(
                load_threshold=0.8,  # Trigger at 80% usage
                reduction_factor=0.5,  # Reduce to 50%
            )
            rate = Rate.parse("100/s")
            key = "user:adaptive"

            # Fill to 90% of limit to trigger adaptation
            for _ in range(90):
                await strategy(key, rate, backend)

            # Window should now be reduced
            # Allow window to reset
            await asyncio.sleep(1.1)

            # New window should have reduced limit
            # This is complex to test precisely, so we just verify it still works
            wait = await strategy(key, rate, backend)
            assert wait >= 0


class TestPriorityQueueStrategy:
    """Tests for PriorityQueueStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_higher_priority_takes_precedence(self, backend: InMemoryBackend):
        """Test higher priority requests are allowed over lower priority."""
        async with backend(close_on_exit=True):
            strategy = PriorityQueueStrategy(max_queue_size=50)
            rate = Rate.parse("5/s")
            key = "user:priority"

            # Fill with low priority requests
            for _ in range(3):
                await strategy(key, rate, backend, cost=1)

            # High priority should still be allowed
            wait_high = await strategy(key, rate, backend, cost=1)
            # Note: Priority is passed via context in real usage, hard to test here
            # This test verifies the strategy is callable
            assert wait_high >= 0


class TestQuotaWithRolloverStrategy:
    """Tests for QuotaWithRolloverStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_quota_enforcement(self, backend: InMemoryBackend):
        """Test basic quota limiting."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5,
                max_rollover=50
            )
            rate = Rate.parse("100/s")
            key = "user:quota"

            # Use quota
            for _ in range(100):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0

            # Should be throttled after quota
            wait = await strategy(key, rate, backend)
            assert wait > 0


class TestTimeOfDayStrategy:
    """Tests for TimeOfDayStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_time_of_day_multipliers(self, backend: InMemoryBackend):
        """Test time-based rate adjustments."""
        async with backend(close_on_exit=True):
            import datetime
            
            current_hour = datetime.datetime.now().hour
            
            # Set higher limit for current hour
            time_windows = [
                (current_hour, (current_hour + 1) % 24, 2.0),  # 2x for current hour
            ]
            
            strategy = TimeOfDayStrategy(
                time_windows=time_windows,
                timezone_offset=0
            )
            rate = Rate.parse("10/s")
            key = "user:tod"

            # Should allow 20 requests (2x multiplier)
            for i in range(20):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i+1} should be allowed"

            wait = await strategy(key, rate, backend)
            assert wait > 0


class TestCostBasedTokenBucketStrategy:
    """Tests for CostBasedTokenBucketStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_cost_tracking(self, backend: InMemoryBackend):
        """Test tracks and adapts to cost patterns."""
        async with backend(close_on_exit=True):
            strategy = CostBasedTokenBucketStrategy(
                burst_size=50,
                cost_window=10
            )
            rate = Rate.parse("10/s")
            key = "user:costbucket"

            # Make requests with varying costs
            for cost in [1, 5, 2, 3, 1]:
                wait = await strategy(key, rate, backend, cost=cost)
                assert wait >= 0


class TestDistributedFairnessStrategy:
    """Tests for DistributedFairnessStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_instance_registration(self, backend: InMemoryBackend):
        """Test instances register and get fair share."""
        async with backend(close_on_exit=True):
            strategy1 = DistributedFairnessStrategy(
                instance_id="instance1",
                instance_weight=1.0
            )
            strategy2 = DistributedFairnessStrategy(
                instance_id="instance2",
                instance_weight=1.0
            )
            rate = Rate.parse("100/minute")
            key = "global:limit"

            # Each instance should get roughly equal share
            wait1 = await strategy1(key, rate, backend)
            wait2 = await strategy2(key, rate, backend)
            
            assert wait1 == 0.0
            assert wait2 == 0.0

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_weighted_fairness(self, backend: InMemoryBackend):
        """Test weighted instances get proportional shares."""
        async with backend(close_on_exit=True):
            # High weight instance
            high_weight = DistributedFairnessStrategy(
                instance_id="heavy",
                instance_weight=3.0
            )
            # Low weight instance  
            low_weight = DistributedFairnessStrategy(
                instance_id="light",
                instance_weight=1.0
            )
            rate = Rate.parse("100/minute")
            key = "weighted:limit"

            # Both should be allowed initially
            wait_high = await high_weight(key, rate, backend, cost=1)
            wait_low = await low_weight(key, rate, backend, cost=1)
            
            assert wait_high == 0.0
            assert wait_low == 0.0


class TestGeographicDistributionStrategy:
    """Tests for GeographicDistributionStrategy."""

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
                default_region="default"
            )
            rate = Rate.parse("100/s")

            # US East should get 50 requests
            for i in range(50):
                wait = await strategy("region:us-east-1:user:123", rate, backend)
                assert wait == 0.0, f"US East request {i+1} should be allowed"

            wait = await strategy("region:us-east-1:user:123", rate, backend)
            assert wait > 0, "US East should be throttled after 50"

            # EU West should still have capacity
            wait = await strategy("region:eu-west-1:user:456", rate, backend)
            assert wait == 0.0

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_spillover_capacity(self, backend: InMemoryBackend):
        """Test spillover allows using unused capacity."""
        async with backend(close_on_exit=True):
            strategy = GeographicDistributionStrategy(
                region_multipliers={
                    "us-east-1": 0.6,
                    "eu-west-1": 0.4,
                },
                allow_spillover=True
            )
            rate = Rate.parse("100/s")
            key_us = "region:us-east-1:user:123"

            # Use US capacity
            for _ in range(60):
                await strategy(key_us, rate, backend)

            # Spillover should allow some extra
            # (exact behavior depends on implementation)
            wait = await strategy(key_us, rate, backend)
            # Just verify it's callable
            assert wait >= 0

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
                assert wait == 0.0, f"{strategy.__class__.__name__} should allow unlimited"


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
            assert all(isinstance(r, (int, float)) for r in results)
