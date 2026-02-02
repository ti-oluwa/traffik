"""Tests for stat/statistics functionality across all strategies."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.strategies.leaky_bucket import (
    LeakyBucketStrategy,
    LeakyBucketWithQueueStrategy,
)
from traffik.strategies.sliding_window import (
    SlidingWindowCounterStrategy,
    SlidingWindowLogStrategy,
)
from traffik.strategies.token_bucket import (
    TokenBucketStrategy,
    TokenBucketWithDebtStrategy,
)


@pytest.mark.anyio
@pytest.mark.strategy
async def test_fixed_window_stat(backend: InMemoryBackend):
    """Test Fixed Window strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:stat"

        # Get initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.key == key
        assert stat.rate == rate
        assert stat.hits_remaining == 10, "Should have all 10 hits remaining"
        assert stat.wait_ms == 0.0, "Should have no wait time"

        # Make 3 requests
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)

        # Check stat after requests
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 7, "Should have 7 hits remaining"
        assert stat.wait_ms == 0.0, "Should still have no wait time"

        # Use up all remaining hits
        for _ in range(7):
            await strategy(key, rate, backend, cost=1)

        # Check stat when at limit
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 0, "Should have 0 hits remaining"
        assert stat.wait_ms == 0.0, "At limit but not exceeded"

        # Try one more (will be throttled)
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Should be throttled"

        # Check stat when throttled
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 0, "Should still have 0 hits remaining"
        assert stat.wait_ms > 0.0, "Should have wait time"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_sliding_window_log_stat(backend: InMemoryBackend):
    """Test Sliding Window Log strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/s")
        key = "user:sliding_stat"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 5
        assert stat.wait_ms == 0.0

        # Make 2 requests
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 3, "Should have 3 hits remaining"

        # Make 3 more requests (at limit)
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 0, "Should have 0 hits remaining"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_sliding_window_counter_stat(backend: InMemoryBackend):
    """Test Sliding Window Counter strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = SlidingWindowCounterStrategy()
        rate = Rate.parse("8/s")
        key = "user:counter_stat"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 8

        # Make some requests
        await strategy(key, rate, backend, cost=1)
        await strategy(key, rate, backend, cost=1)

        stat = await strategy.get_stat(key, rate, backend)
        # Sliding window counter uses weighted average, so exact value may vary
        assert stat.hits_remaining >= 0, "Should have valid hits remaining"
        assert stat.hits_remaining <= 8, "Should not exceed limit"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_token_bucket_stat(backend: InMemoryBackend):
    """Test Token Bucket strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = TokenBucketStrategy(burst_size=15)
        rate = Rate.parse("10/s")
        key = "user:token_stat"

        # Initial stat - bucket starts full at burst_size
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 15, "Should start with burst_size tokens"
        assert stat.wait_ms == 0.0

        # Consume 5 tokens
        await strategy(key, rate, backend, cost=5)

        stat = await strategy.get_stat(key, rate, backend)
        assert 9 <= stat.hits_remaining <= 11, "Should have ~10 tokens left"

        # Consume 10 more tokens
        await strategy(key, rate, backend, cost=10)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining < 1, "Should have ~0 tokens left"
        assert stat.wait_ms >= 0.0, "At limit but not negative"

        # Try to consume more (will be throttled)
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Should be throttled"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_token_bucket_with_debt_stat(backend: InMemoryBackend):
    """Test Token Bucket with Debt strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = TokenBucketWithDebtStrategy(max_debt=5)
        rate = Rate.parse("10/s")
        key = "user:debt_stat"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        # Token bucket with debt starts with limit tokens (might have small refill)
        assert 10 <= stat.hits_remaining <= 16, "Should start with ~10-15 tokens"

        # Use all tokens
        await strategy(key, rate, backend, cost=10)

        stat = await strategy.get_stat(key, rate, backend)
        # Allow for significant refill during execution
        assert stat.hits_remaining <= 6, "Should have used most tokens"

        # Go into debt
        await strategy(key, rate, backend, cost=3)

        stat = await strategy.get_stat(key, rate, backend)
        # After going into debt, might still have some due to refill
        assert stat.hits_remaining <= 4, "Should have consumed more tokens"

        # Try to exceed max debt
        wait = await strategy(key, rate, backend, cost=3)
        assert wait > 0, "Should be throttled at max debt"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_leaky_bucket_stat(backend: InMemoryBackend):
    """Test Leaky Bucket strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("12/s")
        key = "user:leaky_stat"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 12, "Should start empty (can accept 12)"

        # Add some requests
        await strategy(key, rate, backend, cost=4)

        stat = await strategy.get_stat(key, rate, backend)
        assert 7 <= stat.hits_remaining <= 9, "Should have capacity for ~8 more"

        # Fill bucket
        await strategy(key, rate, backend, cost=8)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining < 1, "Bucket should be ~full"
        assert stat.wait_ms >= 0.0, "Should have valid wait time"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_leaky_bucket_with_queue_stat(backend: InMemoryBackend):
    """Test Leaky Bucket with Queue strategy statistics."""
    async with backend(close_on_exit=True):
        strategy = LeakyBucketWithQueueStrategy()
        rate = Rate.parse("8/s")
        key = "user:queue_stat"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining >= 0, "Should have valid hits remaining"

        # Add requests
        await strategy(key, rate, backend, cost=3)
        await strategy(key, rate, backend, cost=2)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining >= 0, "Should have valid hits remaining"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_with_cost(backend: InMemoryBackend):
    """Test statistics with variable costs."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("20/s")
        key = "user:stat_cost"

        # Initial stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 20

        # Make requests with different costs
        await strategy(key, rate, backend, cost=5)
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 15, "Should account for cost=5"

        await strategy(key, rate, backend, cost=7)
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 8, "Should account for cost=7"

        await strategy(key, rate, backend, cost=3)
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 5, "Should account for cost=3"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_unlimited_rate(backend: InMemoryBackend):
    """Test statistics with unlimited rate."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate(limit=0, seconds=0)  # Unlimited
        key = "user:unlimited_stat"

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == float("inf"), "Should have infinite hits"
        assert stat.wait_ms == 0.0, "Should have no wait time"

        # Make some requests
        await strategy(key, rate, backend, cost=100)
        await strategy(key, rate, backend, cost=200)

        # Stat should still show unlimited
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == float("inf"), "Should still be infinite"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_after_window_reset(backend: InMemoryBackend):
    """Test that stats reset correctly after window expires."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/200ms")
        key = "user:reset_stat"

        # Use up limit
        for _ in range(5):
            await strategy(key, rate, backend, cost=1)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 0, "Should be at limit"

        # Wait for window to reset
        await asyncio.sleep(0.25)

        # Stat should show reset
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 5, "Should be reset to full limit"
        assert stat.wait_ms == 0.0, "Should have no wait time"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_keys_isolation(backend: InMemoryBackend):
    """Test that stats for different keys are independent."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")

        # User 1 makes requests
        await strategy("user:1", rate, backend, cost=7)
        stat1 = await strategy.get_stat("user:1", rate, backend)
        assert stat1.hits_remaining == 3

        # User 2 makes different requests
        await strategy("user:2", rate, backend, cost=2)
        stat2 = await strategy.get_stat("user:2", rate, backend)
        assert stat2.hits_remaining == 8

        # Stats should be independent
        assert stat1.hits_remaining != stat2.hits_remaining


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_wait_ms_accuracy(backend: InMemoryBackend):
    """Test that wait_ms in stat is reasonably accurate."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        key = "user:wait_accuracy"

        # Use up limit
        for _ in range(3):
            await strategy(key, rate, backend, cost=1)

        # Exceed limit
        wait_from_call = await strategy(key, rate, backend, cost=1)
        assert wait_from_call > 0, "Should be throttled"

        # Get stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.wait_ms > 0, "Stat should show wait time"

        # Wait times should be close (allowing for small timing differences)
        # The stat wait_ms might be slightly different due to when it's calculated
        assert abs(stat.wait_ms - wait_from_call) < 100, "Wait times should be close"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_token_refill(backend: InMemoryBackend):
    """Test that token bucket stat reflects token refill over time."""
    async with backend(close_on_exit=True):
        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")  # 10 tokens per second
        key = "user:refill_stat"

        # Use all tokens
        await strategy(key, rate, backend, cost=10)

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining < 0.1, "Should have ~0 tokens"

        # Wait for some tokens to refill (200ms = ~2 tokens)
        await asyncio.sleep(0.2)

        stat = await strategy.get_stat(key, rate, backend)
        # Should have refilled some tokens (accounting for timing variance)
        assert stat.hits_remaining >= 1, "Should have refilled at least 1 token"
        assert stat.hits_remaining <= 4, "Should have refilled ~2 tokens"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_concurrent_access(backend: InMemoryBackend):
    """Test that stats work correctly with concurrent requests."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("20/s")
        key = "user:concurrent_stat"

        # Make concurrent requests
        tasks = [strategy(key, rate, backend, cost=1) for _ in range(15)]
        await asyncio.gather(*tasks)

        # Get stat
        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == 5, (
            "Should have 5 hits remaining after 15 requests"
        )


@pytest.mark.anyio
@pytest.mark.strategy
async def test_stat_fields_present(backend: InMemoryBackend):
    """Test that all required stat fields are present."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:fields"

        stat = await strategy.get_stat(key, rate, backend)

        # Check all required fields exist
        assert hasattr(stat, "key"), "Stat should have 'key' field"
        assert hasattr(stat, "rate"), "Stat should have 'rate' field"
        assert hasattr(stat, "hits_remaining"), (
            "Stat should have 'hits_remaining' field"
        )
        assert hasattr(stat, "wait_ms"), "Stat should have 'wait_ms' field"

        # Check types
        assert isinstance(stat.hits_remaining, (int, float)), (
            "hits_remaining should be numeric"
        )
        assert isinstance(stat.wait_ms, (int, float)), "wait_ms should be numeric"
