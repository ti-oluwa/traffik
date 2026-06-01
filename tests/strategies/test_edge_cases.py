"""Tests for edge cases and error scenarios."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.strategies.leaky_bucket import LeakyBucketStrategy
from traffik.strategies.sliding_window import SlidingWindowLogStrategy
from traffik.strategies.token_bucket import TokenBucketStrategy


@pytest.mark.anyio
@pytest.mark.strategy
class TestRateEdgeCases:
    """Tests for edge cases in rate definitions."""

    async def test_very_small_rate_limit(self, backend: InMemoryBackend):
        """Test with rate limit of 1."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("1/s")
        key = "user:one"

        # First request succeeds
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "First request should succeed"

        # Second request throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Second request should be throttled"

    async def test_very_large_rate_limit(self, backend: InMemoryBackend):
        """Test with very large rate limit."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10000/s")
        key = "user:large"

        # Should handle large limits
        for i in range(100):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should succeed with large limit"

    async def test_very_short_time_window(self, backend: InMemoryBackend):
        """Test with very short time window (milliseconds)."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/50ms")
        key = "user:short"

        # Should handle short windows
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should succeed"

        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should throttle after limit"
        assert wait <= 50, "Wait should be within window duration"

    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_very_long_time_window(self, backend: InMemoryBackend):
        """Test with very long time window."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("100/3600s")  # 100 per hour
        key = "user:long"

        # Should handle long windows
        for i in range(100):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should succeed"

        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should throttle after limit"

    async def test_fractional_rates(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate(limit=1, seconds=2)
        key = "user:fraction"

        # First request succeeds
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "First request should succeed"

        # Second request throttled for 2 seconds
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled"

        # Wait for window to expire
        await asyncio.sleep(2.1)

        # Should be allowed again
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should be allowed after 2 seconds"


@pytest.mark.anyio
@pytest.mark.strategy
class TestKeyEdgeCases:
    """Tests for edge cases with throttling keys."""

    async def test_empty_key(self, backend: InMemoryBackend):
        """Test with empty string key."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = ""

        # Should handle empty key
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should handle empty key"

    async def test_very_long_key(self, backend: InMemoryBackend):
        """Test with very long key string."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:" + "x" * 1000  # 1000+ character key

        # Should handle long keys
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should handle long key"

    async def test_special_characters_in_key(self, backend: InMemoryBackend):
        """Test keys with special characters."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")

        special_keys = [
            "user:123@example.com",
            "ip:192.168.1.1",
            "path:/api/v1/users",
            "user:name with spaces",
            "user:emoji:😀",
            "user:unicode:日本語",
            "user:special:!@#$%^&*()",
        ]

        for key in special_keys:
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Should handle key: {key}"

    async def test_numeric_key(self, backend: InMemoryBackend):
        """Test with numeric key (will be converted to string)."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = 12345

        # Should handle numeric keys
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should handle numeric key"


@pytest.mark.anyio
@pytest.mark.strategy
class TestConcurrencyEdgeCases:
    """Tests for concurrency edge cases."""

    async def test_extreme_concurrency(self, backend: InMemoryBackend):
        """Test with very high concurrency request count."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("100/s")
        key = "user:extreme"

        # Make 100 concurrent requests
        results = await asyncio.gather(
            *[strategy(key, rate, backend) for _ in range(100)]
        )

        # Exactly 100 should succeed
        allowed = sum(1 for wait in results if wait == 0.0)
        assert allowed == 100, f"Expected 100 allowed, got {allowed}"

        # Next request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "101st request should be throttled"

    async def test_race_condition_at_boundary(self, backend: InMemoryBackend):
        """Test concurrent requests right at rate limit boundary."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:race"

        # Use up 9 requests
        for _ in range(9):
            await strategy(key, rate, backend)

        # Make 2 concurrent requests for the last spot
        results = await asyncio.gather(
            strategy(key, rate, backend),
            strategy(key, rate, backend),
        )

        # One should succeed, one should fail
        allowed = sum(1 for wait in results if wait == 0.0)
        throttled = sum(1 for wait in results if wait > 0)

        assert allowed == 1, "Exactly one request should succeed"
        assert throttled == 1, "Exactly one request should be throttled"

    async def test_concurrent_different_keys(self, backend: InMemoryBackend):
        """Test concurrent requests for different keys."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")

        # Make concurrent requests for 10 different keys
        tasks = []
        for i in range(10):
            for _ in range(5):
                tasks.append(strategy(f"user:{i}", rate, backend))

        results = await asyncio.gather(*tasks)

        # All should succeed (different keys)
        allowed = sum(1 for wait in results if wait == 0.0)
        assert allowed == 50, f"All 50 requests should succeed, got {allowed}"


@pytest.mark.anyio
@pytest.mark.strategy
class TestTimingEdgeCases:
    """Tests for timing-related edge cases."""

    async def test_rapid_sequential_requests(self, backend: InMemoryBackend):
        """Test many sequential requests as fast as possible."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:rapid"

        # Make 10 requests rapidly
        for i in range(10):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should succeed"

        # 11th should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "11th request should be throttled"

    async def test_requests_across_multiple_windows(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/50ms")
        key = "user:multiwindow"

        for window in range(5):
            # Make requests in each window
            for i in range(3):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Window {window} request {i + 1} should succeed"

            # Wait for next window
            await asyncio.sleep(0.06)


@pytest.mark.anyio
@pytest.mark.strategy
class TestStrategyStateEdgeCases:
    """Tests for strategy state management edge cases."""

    async def test_strategy_with_corrupted_state(self, backend: InMemoryBackend):
        """Test strategy recovery from corrupted state."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupted"

        # Make normal request
        await strategy(key, rate, backend)

        # Corrupt the state by setting invalid data
        state_key = backend.get_key(f"{key}:fixedwindow")
        await backend.set(state_key, "invalid_json_data", expire=1000)

        # Should recover gracefully
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from corrupted state"

    async def test_multiple_strategies_same_key(self, backend: InMemoryBackend):
        """Test different strategies operating on the same key namespace."""
        fixed = FixedWindowStrategy()
        sliding = SlidingWindowLogStrategy()
        token = TokenBucketStrategy()
        leaky = LeakyBucketStrategy()

        rate = Rate.parse("5/s")
        key = "user:multi"

        # Each strategy should maintain independent state
        for strategy in [fixed, sliding, token, leaky]:
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"{strategy.__class__.__name__} request {i + 1} should succeed"
                )

    async def test_backend_connection_during_request(self, backend: InMemoryBackend):
        """Test strategy behavior when backend is available."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:connection"

        # Normal operation
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should succeed with connected backend"


@pytest.mark.anyio
@pytest.mark.strategy
class TestWaitTimeCalculations:
    """Tests for wait time calculation edge cases."""

    async def test_wait_ms_never_negative(self, backend: InMemoryBackend):
        """Ensure wait time is never negative."""
        strategies = [
            FixedWindowStrategy(),
            SlidingWindowLogStrategy(),
            TokenBucketStrategy(),
            LeakyBucketStrategy(),
        ]
        rate = Rate.parse("3/s")

        for i, strategy in enumerate(strategies):
            key = f"wait:{i}"

            # Use up limit
            for _ in range(3):
                await strategy(key, rate, backend)

            # Check wait time
            wait = await strategy(key, rate, backend)
            assert wait >= 0, (
                f"{strategy.__class__.__name__} returned negative wait: {wait}"
            )

    async def test_wait_ms_reasonable_bounds(self, backend: InMemoryBackend):
        """Ensure wait time stays within reasonable bounds."""
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:bounds"

        # Use up limit
        for _ in range(5):
            await strategy(key, rate, backend)

        # Wait time should not exceed window duration significantly
        wait = await strategy(key, rate, backend)
        assert 0 < wait <= 1500, f"Wait time {wait}ms out of reasonable bounds"

    async def test_wait_ms_decreases_over_time(self, backend: InMemoryBackend):

        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:decrease"

        # Use all tokens
        for _ in range(10):
            await strategy(key, rate, backend)

        # Get initial wait time
        wait1 = await strategy(key, rate, backend)
        assert wait1 > 0, "Should be throttled"

        # Wait a bit
        await asyncio.sleep(0.2)

        # Wait time should be less (or request allowed)
        wait2 = await strategy(key, rate, backend)
        assert wait2 <= wait1, "Wait time should decrease or become 0"
