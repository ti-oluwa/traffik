"""Tests for Fixed Window strategy."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_basic_limiting(backend: InMemoryBackend):
    """Test basic rate limiting with fixed window strategy."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        key = "user:123"

        # First 3 requests should succeed
        for i in range(3):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed"

        # 4th request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Request 4 should be throttled"
        assert wait <= rate.expire, "Wait time should not exceed window duration"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_reset(backend: InMemoryBackend):
    """Test that counter resets after window expires."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("2/100ms")
        key = "user:456"

        # Use up limit
        await strategy(key, rate, backend)
        await strategy(key, rate, backend)

        # Should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled"

        # Wait for window to expire
        wait_sec = (rate.expire / 1000) + 0.1  # Extra 0.1s to ensure expiration
        await asyncio.sleep(wait_sec)

        # Should be allowed again
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should be allowed after window reset"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_different_keys(backend: InMemoryBackend):
    """Test that different keys are tracked independently."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("2/s")

        # User 1 uses up their limit
        await strategy("user:1", rate, backend)
        await strategy("user:1", rate, backend)
        wait = await strategy("user:1", rate, backend)
        assert wait > 0, "User 1 should be throttled"

        # User 2 should still be allowed
        wait = await strategy("user:2", rate, backend)
        assert wait == 0.0, "User 2 should not be affected"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_unlimited_rate(backend: InMemoryBackend):
    """Test that unlimited rates always allow requests."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate(limit=0, seconds=0)
        key = "user:unlimited"

        # Should always be allowed
        for _ in range(100):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, "Unlimited rate should always allow requests"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_concurrent_requests(backend: InMemoryBackend):
    """Test strategy under concurrent load."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:concurrent"

        # Make 10 concurrent requests
        results = await asyncio.gather(
            *[strategy(key, rate, backend) for _ in range(10)]
        )

        # All 10 should succeed
        allowed = sum(1 for wait in results if wait == 0.0)
        assert allowed == 10, "All 10 requests should be allowed"

        # 11th request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "11th request should be throttled"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_boundary_burst(backend: InMemoryBackend):
    """Test that fixed window allows bursts at boundaries."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/100ms")
        key = "user:boundary"

        # Use up first window
        wait_ms = 0
        for _ in range(5):
            wait_ms = await strategy(key, rate, backend)

        # Wait for next window
        await asyncio.sleep(max(wait_ms // 1000, 1))  # The

        # Should be able to make 5 more requests immediately
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} in new window should be allowed"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_wait_time_accuracy(backend: InMemoryBackend):
    """Test that wait time calculation is accurate."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("1/200ms")
        key = "user:wait"

        # First request succeeds
        await strategy(key, rate, backend)

        # Second request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled"
        assert wait <= 200, f"Wait time {wait}ms should not exceed window 200ms"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_multiple_windows(backend: InMemoryBackend):
    """Test behavior across multiple time windows."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("2/200ms")
        key = "user:multiwindow"

        for window in range(3):
            # Use up limit in each window
            await strategy(key, rate, backend)
            await strategy(key, rate, backend)

            # Should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, f"Should be throttled in window {window + 1}"

            # Wait for next window. Next window starts at (window + 1) * 100ms
            await asyncio.sleep(0.11)  # 110ms to ensure new window


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_sub_second_rates(backend: InMemoryBackend):
    """Test with rates smaller than 1 second."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/100ms")
        key = "user:subsecond"

        # Should allow 5 requests
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed"

        # 6th should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Request 6 should be throttled"
        assert wait <= 100, "Wait should be within window duration"
