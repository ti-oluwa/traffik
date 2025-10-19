"""Tests for Sliding Window strategies."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.sliding_window import (
    SlidingWindowCounterStrategy,
    SlidingWindowLogStrategy,
)


class TestSlidingWindowLogStrategy:
    """Tests for `SlidingWindowLogStrategy`."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with sliding window log."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate.parse("3/s")
            key = "user:123"

            # First 3 requests should succeed
            for i in range(3):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed"

            # 4th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request 4 should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_sliding_window_behavior(self, backend: InMemoryBackend):
        """Test that window truly slides (no boundary bursts)."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate.parse("5/500ms")
            key = "user:sliding"

            # Make 5 requests at start
            for _ in range(5):
                await strategy(key, rate, backend)

            # Wait 250ms (half window)
            await asyncio.sleep(0.25)

            # Should still be throttled (window hasn't cleared)
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled within window"

            # Wait another 300ms (total 550ms, past first request)
            await asyncio.sleep(0.3)

            # Now should be allowed (oldest request expired)
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, "Should be allowed after oldest request expires"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_accurate_rate_enforcement(self, backend: InMemoryBackend):
        """Test that rate is enforced accurately over sliding window."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate.parse("10/s")
            key = "user:accurate"

            # Make 10 requests
            for _ in range(10):
                await strategy(key, rate, backend)

            # Wait 100ms
            await asyncio.sleep(0.1)

            # Should still be at capacity
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should still be at capacity"

            # Wait another 900ms (total 1s from first request)
            await asyncio.sleep(0.9)

            # Now should allow new requests
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, "Should allow after 1s from first request"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate.parse("20/s")
            key = "user:concurrent"

            # Make 20 concurrent requests
            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(20)]
            )

            # All 20 should succeed
            allowed = sum(1 for wait in results if wait == 0.0)
            assert allowed == 20, f"All 20 requests should be allowed, got {allowed}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_keys_isolation(self, backend: InMemoryBackend):
        """Test that different keys are tracked independently."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate.parse("2/s")

            # User 1 uses up limit
            await strategy("user:1", rate, backend)
            await strategy("user:1", rate, backend)
            wait = await strategy("user:1", rate, backend)
            assert wait > 0, "User 1 should be throttled"

            # User 2 should be unaffected
            wait = await strategy("user:2", rate, backend)
            assert wait == 0.0, "User 2 should not be affected"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowLogStrategy()
            rate = Rate(limit=0, seconds=0)
            key = "user:unlimited"

            for _ in range(50):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, "Unlimited rate should always allow"


class TestSlidingWindowCounterStrategy:
    """Tests for SlidingWindowCounterStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with sliding window counter."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("5/s")
            key = "user:123"

            # First 5 requests should succeed
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed"

            # 6th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request 6 should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_weighted_calculation(self, backend: InMemoryBackend):
        """Test that sliding window counter uses weighted calculation across windows."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("10/1s")
            key = "user:weighted"

            # Make 10 requests to fill the current window
            for _ in range(10):
                await strategy(key, rate, backend)

            # Should be at limit
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled at limit"

            # Wait just over 1 window to move to next window
            # Then wait a bit more so we're partway through that window
            # Total: ~1.2s = 1.2 windows
            await asyncio.sleep(1.2)

            # At this point we're in a new window with the previous window
            # (containing 10 requests) weighted into the calculation.
            # The exact number allowed depends on timing, but it should be
            # less than the full limit of 10 due to weighted calculation.
            allowed = 0
            for i in range(15):  # Try more than the limit
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed += 1
                else:
                    break

            # Should allow fewer than 10 requests due to weighted calculation
            # from previous window. But should allow at least 1.
            assert 1 <= allowed < 10, (
                f"Expected 1-9 requests allowed (weighted calc), got {allowed}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_better_than_fixed_window(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("5/200ms")
            key = "user:boundary"

            # Make 5 requests
            for _ in range(5):
                await strategy(key, rate, backend)

            # Wait 100ms (halfway)
            await asyncio.sleep(0.1)

            # With sliding window counter, should still be limited
            # (not a full reset like fixed window)
            allowed_count = 0
            for _ in range(5):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1

            # Should allow fewer than 5 (better than fixed window's full reset)
            assert allowed_count < 5, "Should prevent full boundary burst"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("15/s")
            key = "user:concurrent"

            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(15)]
            )

            allowed = sum(1 for wait in results if wait == 0.0)
            assert allowed == 15, f"All 15 requests should be allowed, got {allowed}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_keys_isolation(self, backend: InMemoryBackend):
        """Test that different keys are tracked independently."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("3/s")

            # User 1 uses up limit
            for _ in range(3):
                await strategy("user:1", rate, backend)
            wait = await strategy("user:1", rate, backend)
            assert wait > 0, "User 1 should be throttled"

            # User 2 should be unaffected
            wait = await strategy("user:2", rate, backend)
            assert wait == 0.0, "User 2 should not be affected"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate(limit=0, seconds=0)
            key = "user:unlimited"

            for _ in range(50):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, "Unlimited rate should always allow"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_window_transitions(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = SlidingWindowCounterStrategy()
            rate = Rate.parse("5/100ms")
            key = "user:transition"

            # Fill first window
            for _ in range(5):
                await strategy(key, rate, backend)

            # Wait for partial window overlap: 50ms
            await asyncio.sleep(0.05)

            # Make more requests - should handle window transition gracefully
            for _ in range(3):
                await strategy(key, rate, backend)

            # Wait for full window reset
            await asyncio.sleep(0.1)

            # Should be allowed again
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, "Should allow after window reset"
