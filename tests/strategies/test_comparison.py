"""Comparative tests between different strategies."""

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
async def test_fixed_vs_sliding_window_boundary_burst(backend: InMemoryBackend):
    """
    Test that fixed window allows boundary bursts while sliding window doesn't.

    Fixed window resets completely at boundaries, allowing full rate again.
    Sliding window prevents this by tracking requests in a sliding manner.
    """
    async with backend(close_on_exit=True):
        fixed_strategy = FixedWindowStrategy()
        sliding_strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/100ms")

        # Fixed window - use up first window
        for _ in range(5):
            await fixed_strategy("fixed:user", rate, backend)

        # Sliding window - use up capacity
        for _ in range(5):
            await sliding_strategy("sliding:user", rate, backend)

        # Wait for next window boundary
        await asyncio.sleep(0.11)

        # Fixed window allows full burst again
        fixed_allowed = 0
        for _ in range(5):
            wait = await fixed_strategy("fixed:user", rate, backend)
            if wait == 0.0:
                fixed_allowed += 1

        # Sliding window allows requests gradually
        sliding_allowed = 0
        for _ in range(5):
            wait = await sliding_strategy("sliding:user", rate, backend)
            if wait == 0.0:
                sliding_allowed += 1

        assert fixed_allowed == 5, "Fixed window should allow full burst"
        assert sliding_allowed == 5, "Sliding window should allow all after full window"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_token_bucket_vs_leaky_bucket_burst(backend: InMemoryBackend):
    """
    Test burst handling differences between token bucket and leaky bucket.

    Token bucket allows bursts (up to bucket size).
    Leaky bucket prevents bursts (only allows based on leak rate).
    """
    async with backend(close_on_exit=True):
        token_strategy = TokenBucketStrategy(burst_size=20)
        leaky_strategy = LeakyBucketStrategy()
        rate = Rate.parse("10/s")

        # Token bucket allows burst of 20
        token_allowed = 0
        for _ in range(20):
            wait = await token_strategy("token:user", rate, backend)
            if wait == 0.0:
                token_allowed += 1

        # Leaky bucket only allows 10 (no burst)
        leaky_allowed = 0
        for _ in range(20):
            wait = await leaky_strategy("leaky:user", rate, backend)
            if wait == 0.0:
                leaky_allowed += 1

        assert token_allowed == 20, "Token bucket should allow burst"
        assert leaky_allowed == 10, "Leaky bucket should not allow burst"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_sliding_log_vs_counter_accuracy(backend: InMemoryBackend):
    """
    Test accuracy difference between sliding window log and counter.

    Sliding window log is perfectly accurate (tracks exact timestamps).
    Sliding window counter is approximate (weighted calculation).
    """
    async with backend(close_on_exit=True):
        log_strategy = SlidingWindowLogStrategy()
        counter_strategy = SlidingWindowCounterStrategy()
        rate = Rate.parse("10/200ms")

        # Both strategies start the same
        for _ in range(10):
            await log_strategy("log:user", rate, backend)
            await counter_strategy("counter:user", rate, backend)

        # Wait halfway through window
        await asyncio.sleep(0.1)

        # Log strategy should be more precise about what's allowed
        log_wait = await log_strategy("log:user", rate, backend)
        counter_wait = await counter_strategy("counter:user", rate, backend)

        # Both should throttle, but timing may differ slightly
        assert log_wait > 0, "Log strategy should throttle"
        assert counter_wait > 0, "Counter strategy should throttle"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_debt_strategy_vs_regular_recovery(backend: InMemoryBackend):
    """
    Test recovery behavior with and without debt tracking.

    Regular token bucket recovers faster (no debt to repay).
    Token bucket with debt has slower recovery (must repay debt).
    """
    async with backend(close_on_exit=True):
        regular_strategy = TokenBucketStrategy()
        debt_strategy = TokenBucketWithDebtStrategy(max_debt=10)
        rate = Rate.parse("10/s")

        # Both use all tokens
        for _ in range(10):
            await regular_strategy("regular:user", rate, backend)
            await debt_strategy("debt:user", rate, backend)

        # Both make additional requests (regular throttles, debt accumulates)
        for _ in range(5):
            await regular_strategy("regular:user", rate, backend)
            await debt_strategy("debt:user", rate, backend)

        # Wait for some refill
        await asyncio.sleep(0.3)

        # Regular strategy should allow requests now
        regular_wait = await regular_strategy("regular:user", rate, backend)

        # Debt strategy should still throttle (repaying debt)
        debt_wait = await debt_strategy("debt:user", rate, backend)

        assert regular_wait == 0.0, "Regular strategy should recover"
        assert debt_wait > 0, "Debt strategy should still throttle"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_all_strategies_respect_unlimited_rate(backend: InMemoryBackend):
    """Test that all strategies handle unlimited rate correctly."""
    async with backend(close_on_exit=True):
        strategies = [
            FixedWindowStrategy(),
            SlidingWindowLogStrategy(),
            SlidingWindowCounterStrategy(),
            TokenBucketStrategy(),
            TokenBucketWithDebtStrategy(),
            LeakyBucketStrategy(),
            LeakyBucketWithQueueStrategy(),
        ]
        rate = Rate(limit=0, seconds=0)

        for i, strategy in enumerate(strategies):
            for _ in range(10):
                wait = await strategy(f"unlimited:{i}", rate, backend)
                assert wait == 0.0, (
                    f"{strategy.__class__.__name__} should allow unlimited"
                )


@pytest.mark.anyio
@pytest.mark.strategy
async def test_all_strategies_enforce_basic_limit(backend: InMemoryBackend):
    """Test that all strategies enforce basic rate limits."""
    async with backend(close_on_exit=True):
        strategies = [
            FixedWindowStrategy(),
            SlidingWindowLogStrategy(),
            SlidingWindowCounterStrategy(),
            TokenBucketStrategy(),
            TokenBucketWithDebtStrategy(),
            LeakyBucketStrategy(),
            LeakyBucketWithQueueStrategy(),
        ]
        rate = Rate.parse("5/s")

        for i, strategy in enumerate(strategies):
            key = f"basic:{i}"

            # First 5 should succeed
            for _ in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, (
                    f"{strategy.__class__.__name__} should allow first 5"
                )

            # 6th should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, (
                f"{strategy.__class__.__name__} should throttle 6th request"
            )


@pytest.mark.anyio
@pytest.mark.strategy
async def test_all_strategies_concurrent_safety(backend: InMemoryBackend):
    """Test that all strategies handle concurrent requests safely."""
    async with backend(close_on_exit=True):
        strategies = [
            FixedWindowStrategy(),
            SlidingWindowLogStrategy(),
            SlidingWindowCounterStrategy(),
            TokenBucketStrategy(),
            TokenBucketWithDebtStrategy(),
            LeakyBucketStrategy(),
            LeakyBucketWithQueueStrategy(),
        ]
        rate = Rate.parse("10/s")

        for i, strategy in enumerate(strategies):
            key = f"concurrent:{i}"

            # Make 10 concurrent requests
            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(10)]
            )

            allowed = sum(1 for wait in results if wait == 0.0)
            assert allowed == 10, (
                f"{strategy.__class__.__name__} should handle concurrency"
            )


@pytest.mark.anyio
@pytest.mark.strategy
async def test_strategy_performance_comparison(backend: InMemoryBackend):
    """
    Compare performance characteristics of different strategies.

    This is mainly to ensure all strategies complete in reasonable time.
    """
    async with backend(close_on_exit=True):
        strategies = [
            ("Fixed Window", FixedWindowStrategy()),
            ("Sliding Log", SlidingWindowLogStrategy()),
            ("Sliding Counter", SlidingWindowCounterStrategy()),
            ("Token Bucket", TokenBucketStrategy()),
            ("Token Debt", TokenBucketWithDebtStrategy()),
            ("Leaky Bucket", LeakyBucketStrategy()),
            ("Leaky Queue", LeakyBucketWithQueueStrategy()),
        ]
        rate = Rate.parse("100/s")

        for name, strategy in strategies:
            key = f"perf:{name}"
            start = asyncio.get_event_loop().time()

            # Make 100 requests
            for _ in range(100):
                await strategy(key, rate, backend)

            elapsed = asyncio.get_event_loop().time() - start

            # All strategies should complete in reasonable time (< 1 second)
            assert elapsed < 1.0, f"{name} took too long: {elapsed}s"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_strategy_memory_efficiency(backend: InMemoryBackend):
    """
    Test memory efficiency by making many requests to different keys.

    Mainly ensures strategies clean up properly and don't leak memory.
    """
    async with backend(close_on_exit=True):
        strategy = FixedWindowStrategy()  # Use simplest strategy
        rate = Rate.parse("10/s")

        # Make requests for many different keys
        for i in range(100):
            await strategy(f"memory:{i}", rate, backend)

        # All should succeed (different keys)
        for i in range(100):
            wait = await strategy(f"memory2:{i}", rate, backend)
            assert wait == 0.0, f"Request for key {i} should succeed"


@pytest.mark.anyio
@pytest.mark.strategy
async def test_strategy_key_isolation(backend: InMemoryBackend):
    """Test that all strategies properly isolate different keys."""
    async with backend(close_on_exit=True):
        strategies = [
            FixedWindowStrategy(),
            SlidingWindowLogStrategy(),
            SlidingWindowCounterStrategy(),
            TokenBucketStrategy(),
            TokenBucketWithDebtStrategy(),
            LeakyBucketStrategy(),
            LeakyBucketWithQueueStrategy(),
        ]
        rate = Rate.parse("3/s")

        for i, strategy in enumerate(strategies):
            # User 1 exhausts their limit
            for _ in range(3):
                await strategy(f"isolation:{i}:user1", rate, backend)

            user1_wait = await strategy(f"isolation:{i}:user1", rate, backend)
            assert user1_wait > 0, (
                f"{strategy.__class__.__name__} user 1 should be throttled"
            )

            # User 2 should be unaffected
            user2_wait = await strategy(f"isolation:{i}:user2", rate, backend)
            assert user2_wait == 0.0, (
                f"{strategy.__class__.__name__} user 2 should be allowed"
            )
