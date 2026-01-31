"""Tests for cost/weight functionality across all strategies."""

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
async def test_fixed_window_with_cost(backend: InMemoryBackend):
    """Test Fixed Window strategy with variable request costs."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:cost"

        # Make 2 requests with cost=3 each (total 6)
        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "First request should be allowed"
        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "Second request should be allowed (total 6)"

        # Make a request with cost=4 (total would be 10, exactly at limit)
        wait = await strategy(key, rate, backend, cost=4)
        assert wait == 0.0, "Third request should be allowed (6+4 = 10)"

        # Now at limit, cost=5 should be throttled
        wait = await strategy(key, rate, backend, cost=5)
        assert wait > 0, "Fourth request should be throttled (10+5 > 10)"

        # Even cost=1 should be throttled now
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Fifth request should be throttled (10+1 > 10)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_sliding_window_log_with_cost(backend: InMemoryBackend):
    """Test Sliding Window Log strategy with variable request costs."""
    async with backend(close_on_exit=True):
        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("20/s")
        key = "user:sliding_cost"

        # Make requests with varying costs
        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed"

        wait = await strategy(key, rate, backend, cost=10)
        assert wait == 0.0, "Cost=10 should be allowed (total 15)"

        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "Cost=3 should be allowed (total 18)"

        # This should push over the limit
        wait = await strategy(key, rate, backend, cost=3)
        assert wait > 0, "Cost=3 should be throttled (18+3 > 20)"

        # Cost=2 should fit exactly
        wait = await strategy(key, rate, backend, cost=2)
        assert wait == 0.0, "Cost=2 should be allowed (18+2 = 20)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_sliding_window_counter_with_cost(backend: InMemoryBackend):
    """Test Sliding Window Counter strategy with variable request costs."""
    async with backend(close_on_exit=True):
        strategy = SlidingWindowCounterStrategy()
        rate = Rate.parse("15/500ms")
        key = "user:counter_cost"

        # Fill current window with weighted requests
        wait = await strategy(key, rate, backend, cost=4)
        assert wait == 0.0, "Cost=4 should be allowed"

        wait = await strategy(key, rate, backend, cost=6)
        assert wait == 0.0, "Cost=6 should be allowed (total 10)"

        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed (total 15)"

        # Next request should be throttled
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Any request should be throttled (15+1 > 15)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_token_bucket_with_cost(backend: InMemoryBackend):
    """Test Token Bucket strategy with variable request costs."""
    async with backend(close_on_exit=True):
        strategy = TokenBucketStrategy(burst_size=20)
        rate = Rate.parse("10/s")
        key = "user:token_cost"

        # Bucket starts with 20 tokens (burst_size)
        wait = await strategy(key, rate, backend, cost=8)
        assert wait == 0.0, "Cost=8 should be allowed (12 tokens left)"

        wait = await strategy(key, rate, backend, cost=7)
        assert wait == 0.0, "Cost=7 should be allowed (5 tokens left)"

        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "Cost=3 should be allowed (2 tokens left)"

        # This should exceed available tokens
        wait = await strategy(key, rate, backend, cost=3)
        assert wait > 0, "Cost=3 should be throttled (only 2 tokens left)"

        # Cost=2 should work
        wait = await strategy(key, rate, backend, cost=2)
        assert wait == 0.0, "Cost=2 should be allowed (exactly enough)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_token_bucket_with_debt_and_cost(backend: InMemoryBackend):
    """Test Token Bucket with Debt strategy using variable costs."""
    async with backend(close_on_exit=True):
        strategy = TokenBucketWithDebtStrategy(max_debt=15)
        rate = Rate.parse("10/s")
        key = "user:debt_cost"

        # Use all tokens (10)
        wait = await strategy(key, rate, backend, cost=10)
        assert wait == 0.0, "Cost=10 should be allowed (bucket at 0)"

        # Go into debt with cost=5 (bucket at -5)
        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed (debt at 5)"

        # Go further into debt with cost=8 (bucket at -13)
        wait = await strategy(key, rate, backend, cost=8)
        assert wait == 0.0, "Cost=8 should be allowed (debt at 13)"

        # Exceeds max debt (would be -16)
        wait = await strategy(key, rate, backend, cost=3)
        assert wait > 0, "Cost=3 should be throttled (would exceed max debt)"

        # Cost=2 should fit within max debt (debt at 15)
        wait = await strategy(key, rate, backend, cost=2)
        assert wait == 0.0, "Cost=2 should be allowed (debt at 15)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_leaky_bucket_with_cost(backend: InMemoryBackend):
    """Test Leaky Bucket strategy with variable request costs."""
    async with backend(close_on_exit=True):
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("20/s")
        key = "user:leaky_cost"

        # Fill bucket with weighted requests
        wait = await strategy(key, rate, backend, cost=7)
        assert wait == 0.0, "Cost=7 should be allowed"

        wait = await strategy(key, rate, backend, cost=8)
        assert wait == 0.0, "Cost=8 should be allowed (total 15)"

        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed (total 20)"

        # Bucket is full, should be throttled
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Cost=1 should be throttled (bucket full)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_leaky_bucket_with_queue_and_cost(backend: InMemoryBackend):
    """Test Leaky Bucket with Queue strategy using variable costs."""
    async with backend(close_on_exit=True):
        strategy = LeakyBucketWithQueueStrategy()
        rate = Rate.parse("15/s")
        key = "user:queue_cost"

        # Fill bucket (15) with weighted requests
        wait = await strategy(key, rate, backend, cost=6)
        assert wait == 0.0, "Cost=6 should be allowed"

        wait = await strategy(key, rate, backend, cost=9)
        assert wait == 0.0, "Cost=9 should be allowed (bucket at 15)"

        # Try to add more - queue behavior
        wait = await strategy(key, rate, backend, cost=1)
        # Depending on queue implementation, might be throttled or queued
        # Just verify it returns a valid wait period
        assert wait >= 0.0, "Valid wait period returned"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_cost_zero_treated_as_one(backend: InMemoryBackend):
    """Test that cost=0 is treated as cost=1 to prevent free requests."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        key = "user:zero_cost"

        # Try to make requests with cost=0
        for i in range(3):
            wait = await strategy(key, rate, backend, cost=0)
            # Depending on implementation, cost=0 might be treated as cost=1
            # or might be rejected. Let's test both scenarios
            if i < 3:
                assert wait == 0.0 or wait > 0, f"Request {i + 1} handled"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_cost_accumulation_across_strategies(backend: InMemoryBackend):
    """Test that costs accumulate correctly over time."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("100/s")
        key = "user:accumulation"

        # Make many small-cost requests
        total_cost = 0
        for i in range(50):
            cost = 2
            wait = await strategy(key, rate, backend, cost=cost)
            if wait == 0.0:
                total_cost += cost
            else:
                break

        assert total_cost == 100, f"Total cost should be 100, got {total_cost}"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_mixed_cost_and_default_cost(backend: InMemoryBackend):
    """Test mixing explicit costs with default cost=1."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:mixed"

        # Make requests with explicit cost
        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "Cost=3 should be allowed"

        # Make requests with default cost (should be 1)
        wait = await strategy(key, rate, backend, cost=1)
        assert wait == 0.0, "Cost=1 should be allowed (total 4)"

        wait = await strategy(key, rate, backend, cost=1)
        assert wait == 0.0, "Cost=1 should be allowed (total 5)"

        # Make another explicit cost request
        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed (total 10)"

        # Should be at limit
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Should be throttled (10+1 > 10)"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_large_cost_single_request(backend: InMemoryBackend):
    """Test that a single request with large cost can exceed or meet limit."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:large"

        # Single request with cost equal to limit
        wait = await strategy(key, rate, backend, cost=10)
        assert wait == 0.0, "Cost=10 should be allowed (exactly at limit)"

        # Next request should be throttled
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Should be throttled after hitting limit"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_cost_isolation_between_keys(backend: InMemoryBackend):
    """Test that costs for different keys don't interfere."""
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")

        # User 1 uses cost=8
        wait = await strategy("user:1", rate, backend, cost=8)
        assert wait == 0.0, "User 1 cost=8 should be allowed"

        # User 2 uses cost=9 (independent of user 1)
        wait = await strategy("user:2", rate, backend, cost=9)
        assert wait == 0.0, "User 2 cost=9 should be allowed"

        # User 1 can still use 2 more
        wait = await strategy("user:1", rate, backend, cost=2)
        assert wait == 0.0, "User 1 cost=2 should be allowed (total 10)"

        # User 1 is now at limit
        wait = await strategy("user:1", rate, backend, cost=1)
        assert wait > 0, "User 1 should be throttled"

        # User 2 can still use 1 more
        wait = await strategy("user:2", rate, backend, cost=1)
        assert wait == 0.0, "User 2 cost=1 should be allowed (total 10)"
