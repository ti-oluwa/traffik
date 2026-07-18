"""Tests for cost/weight functionality across all strategies."""

import pytest
from starlette.requests import HTTPConnection

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.token_bucket import TokenBucketWithDebtStrategy
from traffik.throttles import ThrottleStrategy


@pytest.mark.anyio
@pytest.mark.strategy
class TestStrategyCost:
    async def test_cost(
        self, backend: InMemoryBackend, strategy: ThrottleStrategy[HTTPConnection]
    ):
        """Test strategy with variable request costs."""
        rate = Rate.parse("10/600ms")
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

    async def test_token_bucket_with_debt_and_cost(self, backend: InMemoryBackend):
        """Test Token Bucket with Debt strategy using variable costs."""
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

    async def test_cost_accumulation_across_strategies(
        self, backend: InMemoryBackend, strategy: ThrottleStrategy[HTTPConnection]
    ):
        """Test that costs accumulate correctly over time."""
        rate = Rate.parse("100/s")
        key = "user:accumulation"

        # Make many small-cost requests
        total_cost = 0
        for _ in range(50):
            cost = 2
            wait = await strategy(key, rate, backend, cost=cost)
            if wait == 0.0:
                total_cost += cost
            else:
                break

        assert total_cost == 100, f"Total cost should be 100, got {total_cost}"

    async def test_mixed_cost_and_default_cost(
        self, backend: InMemoryBackend, strategy: ThrottleStrategy[HTTPConnection]
    ):
        """Test mixing explicit costs with default cost=1."""
        rate = Rate.parse("10/s")
        key = "user:mixed"

        # Make requests with explicit cost
        wait = await strategy(key, rate, backend, cost=3)
        assert wait == 0.0, "Cost=3 should be allowed"

        # Make requests with default cost (should be 1)
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Cost=1 should be allowed (total 4)"

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Cost=1 should be allowed (total 5)"

        # Make another explicit cost request
        wait = await strategy(key, rate, backend, cost=5)
        assert wait == 0.0, "Cost=5 should be allowed (total 10)"

        # Should be at limit
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled (10+1 > 10)"

    async def test_large_cost_single_request(
        self, backend: InMemoryBackend, strategy: ThrottleStrategy[HTTPConnection]
    ):
        """Test that a single request with large cost can exceed or meet limit."""
        rate = Rate.parse("10/s")
        key = "user:large"

        # Single request with cost equal to limit
        wait = await strategy(key, rate, backend, cost=10)
        assert wait == 0.0, "Cost=10 should be allowed (exactly at limit)"

        # Next request should be throttled
        wait = await strategy(key, rate, backend, cost=1)
        assert wait > 0, "Should be throttled after hitting limit"

    async def test_cost_isolation_between_keys(
        self, backend: InMemoryBackend, strategy: ThrottleStrategy[HTTPConnection]
    ):
        """Test that costs for different keys don't interfere."""
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
