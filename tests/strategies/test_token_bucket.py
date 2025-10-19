"""Tests for Token Bucket strategies."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.token_bucket import (
    TokenBucketStrategy,
    TokenBucketWithDebtStrategy,
)


class TestTokenBucketStrategy:
    """Tests for TokenBucketStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with token bucket."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("5/s")
            key = "user:123"

            # First 5 requests should succeed (bucket starts full)
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed"

            # 6th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request 6 should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_token_refill(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("10/s")  # 10 tokens per second
            key = "user:refill"

            # Use all tokens
            for _ in range(10):
                await strategy(key, rate, backend)

            # Should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled after using all tokens"

            # Wait for some tokens to refill (500ms = ~5 tokens)
            await asyncio.sleep(0.5)

            # Should allow some requests now
            allowed_count = 0
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            # Should allow at least 4 requests (accounting for timing variance)
            assert allowed_count >= 4, (
                f"Should allow ~5 requests after refill, got {allowed_count}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_burst_handling(self, backend: InMemoryBackend):
        """Test burst capacity handling."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy(burst_size=20)
            rate = Rate.parse("10/s")  # 10 per second, but bucket holds 20
            key = "user:burst"

            # Should allow burst of 20 requests immediately
            for i in range(20):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Burst request {i + 1} should be allowed"

            # 21st request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request beyond burst should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_no_burst_allowance(self, backend: InMemoryBackend):
        """Test with burst_size equal to limit (no extra burst)."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy(burst_size=None)  # Defaults to rate.limit
            rate = Rate.parse("5/s")
            key = "user:noburst"

            # Should allow exactly 5 requests
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i + 1} should be allowed"

            # 6th should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "6th request should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_gradual_refill(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("100/s")  # 100 tokens per second = 1 per 10ms
            key = "user:gradual"

            # Use all tokens
            for _ in range(100):
                await strategy(key, rate, backend)

            # Wait 50ms (should refill ~5 tokens)
            await asyncio.sleep(0.05)

            # Should allow ~5 requests
            allowed_count = 0
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            assert allowed_count >= 4, f"Should allow ~5 requests, got {allowed_count}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("20/s")
            key = "user:concurrent"

            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(20)]
            )

            allowed = sum(1 for wait in results if wait == 0.0)
            assert allowed == 20, f"All 20 requests should be allowed, got {allowed}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_keys_isolation(self, backend: InMemoryBackend):
        """Test that different keys have independent buckets."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("3/s")

            # User 1 uses all tokens
            for _ in range(3):
                await strategy("user:1", rate, backend)
            wait = await strategy("user:1", rate, backend)
            assert wait > 0, "User 1 should be throttled"

            # User 2 should have full bucket
            for i in range(3):
                wait = await strategy("user:2", rate, backend)
                assert wait == 0.0, f"User 2 request {i + 1} should be allowed"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate(limit=0, seconds=0)
            key = "user:unlimited"

            for _ in range(50):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, "Unlimited rate should always allow"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_wait_time_calculation(self, backend: InMemoryBackend):
        """Test that wait time is calculated correctly."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy()
            rate = Rate.parse("10/s")  # 1 token per 100ms
            key = "user:wait"

            # Use all tokens
            for _ in range(10):
                await strategy(key, rate, backend)

            # Next request should need to wait
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should need to wait"
            assert wait <= 1000, "Wait should be within reasonable bounds"


class TestTokenBucketWithDebtStrategy:
    """Tests for TokenBucketWithDebtStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with debt tracking."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy()
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
    async def test_debt_tracking(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy(max_debt=10)
            rate = Rate.parse("10/s")
            key = "user:debt"

            # Use all tokens (bucket at 0)
            for _ in range(10):
                await strategy(key, rate, backend)

            # Go into max debt (bucket at -10)
            for _ in range(10):
                await strategy(key, rate, backend)

            # Now at max debt, next request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should be throttled at max debt"

            # Wait for some refill (0.5s = 5 tokens, bucket at -5)
            await asyncio.sleep(0.5)

            # Should allow some requests now as debt partially paid
            allowed_count = 0
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            # Should allow at least 4 requests (accounting for timing variance)
            assert allowed_count >= 4, (
                f"Should allow ~5 requests after partial refill, got {allowed_count}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_max_debt_limit(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy(max_debt=5)
            rate = Rate.parse("10/s")
            key = "user:maxdebt"

            # Use all tokens
            for _ in range(10):
                await strategy(key, rate, backend)

            # Try to accumulate more debt than allowed
            for _ in range(10):
                await strategy(key, rate, backend)

            # Debt should be capped
            # After enough time, should recover
            await asyncio.sleep(1.5)

            wait = await strategy(key, rate, backend)
            assert wait == 0.0, "Should recover after debt is paid off"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_smoother_recovery(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy(max_debt=10)
            rate = Rate.parse("10/s")
            key = "user:smooth"

            # Use all tokens (bucket at 0)
            for _ in range(10):
                await strategy(key, rate, backend)

            # Go to max debt (bucket at -10)
            for _ in range(10):
                await strategy(key, rate, backend)

            # Should be throttled at max debt
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should throttle at max debt"

            # Wait for partial refill (0.3s = 3 tokens, bucket at -7)
            await asyncio.sleep(0.3)

            # Can now make a few requests before hitting debt limit again
            allowed_count = 0
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            # Should allow 2-3 requests (bucket -7 → -10)
            assert allowed_count >= 2, (
                f"Should allow 2-3 requests after partial refill, got {allowed_count}"
            )

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy()
            rate = Rate.parse("15/s")
            key = "user:concurrent"

            results = await asyncio.gather(
                *[strategy(key, rate, backend) for _ in range(15)]
            )

            allowed = sum(1 for wait in results if wait == 0.0)
            assert allowed == 15, f"All 15 requests should be allowed, got {allowed}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy()
            rate = Rate(limit=0, seconds=0)
            key = "user:unlimited"

            for _ in range(50):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, "Unlimited rate should always allow"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_keys_isolation(self, backend: InMemoryBackend):
        """Test that different keys track debt independently."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketWithDebtStrategy(max_debt=5)
            rate = Rate.parse("3/s")

            # User 1 accumulates debt
            for _ in range(8):
                await strategy("user:1", rate, backend)

            wait = await strategy("user:1", rate, backend)
            assert wait > 0, "User 1 should be throttled"

            # User 2 should be unaffected
            wait = await strategy("user:2", rate, backend)
            assert wait == 0.0, "User 2 should not be affected"
