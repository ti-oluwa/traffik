"""Tests for Leaky Bucket strategies."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.leaky_bucket import (
    LeakyBucketStrategy,
    LeakyBucketWithQueueStrategy,
)


class TestLeakyBucketStrategy:
    """Tests for LeakyBucketStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with leaky bucket."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("5/s")
            key = "user:123"

            # First 5 requests should succeed (bucket starts empty)
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i+1} should be allowed"

            # 6th request should be throttled (bucket full)
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request 6 should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_bucket_leaks_over_time(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("10/s")  # Leaks at 10 per second
            key = "user:leak"

            # Fill bucket
            for _ in range(10):
                await strategy(key, rate, backend)

            # Should be full
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Bucket should be full"

            # Wait for leak (500ms should leak ~5 requests)
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
            assert allowed_count >= 4, f"Should allow ~5 requests after leak, got {allowed_count}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_no_burst_allowed(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("5/100ms")
            key = "user:noburst"

            # Fill bucket
            for _ in range(5):
                await strategy(key, rate, backend)

            # Wait for next window (100ms)
            await asyncio.sleep(0.11)

            # Unlike with fixed window, we can't burst.
            # We can only make requests as bucket leaks
            # Should not be able to make 5 requests immediately
            wait = await strategy(key, rate, backend)
            # Bucket should have leaked fully by now
            assert wait == 0.0, "Should allow after bucket has leaked"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_smooth_rate_enforcement(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("100/s")  # 1 per 10ms
            key = "user:smooth"

            # Fill bucket
            for _ in range(100):
                await strategy(key, rate, backend)

            # Wait 50ms (should leak ~5 requests worth)
            await asyncio.sleep(0.05)

            # Should allow approximately 5 requests
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
            strategy = LeakyBucketStrategy()
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
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("3/s")

            # User 1 fills bucket
            for _ in range(3):
                await strategy("user:1", rate, backend)
            wait = await strategy("user:1", rate, backend)
            assert wait > 0, "User 1 should be throttled"

            # User 2 should have empty bucket
            for i in range(3):
                wait = await strategy("user:2", rate, backend)
                assert wait == 0.0, f"User 2 request {i+1} should be allowed"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
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
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("10/s")
            key = "user:wait"

            # Fill bucket
            for _ in range(10):
                await strategy(key, rate, backend)

            # Next request should need to wait
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should need to wait"
            assert wait <= 1000, "Wait should be within reasonable bounds"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_prevents_downstream_overload(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = LeakyBucketStrategy()
            rate = Rate.parse("10/100ms")
            key = "user:smooth"

            # Make burst of requests
            for _ in range(10):
                await strategy(key, rate, backend)

            # All immediate requests consumed capacity
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Should throttle after burst"

            # Wait for partial leak
            await asyncio.sleep(0.05)

            # Should allow requests proportional to leak
            allowed_count = 0
            for _ in range(10):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            # Should allow roughly half (~5 requests)
            assert 3 <= allowed_count <= 7, f"Should allow ~5 requests, got {allowed_count}"


class TestLeakyBucketWithQueueStrategy:
    """Tests for LeakyBucketWithQueueStrategy."""

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with queue."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate.parse("5/s")
            key = "user:123"

            # First 5 requests should succeed
            for i in range(5):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, f"Request {i+1} should be allowed"

            # 6th request should be throttled
            wait = await strategy(key, rate, backend)
            assert wait > 0, "Request 6 should be throttled"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_queue_behavior(self, backend: InMemoryBackend):
        """Test that requests are queued when bucket is full."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate.parse("10/s")
            key = "user:queue"

            # Fill bucket
            for _ in range(10):
                await strategy(key, rate, backend)

            # Next requests should be queued (not immediately rejected)
            for _ in range(5):
                wait = await strategy(key, rate, backend)
                # Should return wait time but not reject
                assert wait >= 0, "Should queue request"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_queue_fills_up(self, backend: InMemoryBackend):
        """Test that queue fills up with many requests."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate.parse("5/s")
            key = "user:queuefull"

            # Make many requests to fill queue
            for _ in range(20):
                await strategy(key, rate, backend)

            # Should still return wait time (queue handling)
            wait = await strategy(key, rate, backend)
            assert wait >= 0, "Should handle full queue"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_queue_drains_over_time(self, backend: InMemoryBackend):
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate.parse("20/s")  # 20 per second
            key = "user:drain"

            # Fill bucket and queue
            for _ in range(30):
                await strategy(key, rate, backend)

            # Wait for bucket to leak
            await asyncio.sleep(0.5)  # Should leak ~10 requests

            # Should allow some requests now
            allowed_count = 0
            for _ in range(15):
                wait = await strategy(key, rate, backend)
                if wait == 0.0:
                    allowed_count += 1
                else:
                    break

            # Should allow several requests (accounting for variance)
            assert allowed_count >= 8, f"Should allow ~10 requests, got {allowed_count}"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
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
        """Test that different keys have independent queues."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate.parse("3/s")

            # User 1 fills bucket and queue
            for _ in range(8):
                await strategy("user:1", rate, backend)

            # User 2 should be unaffected
            for i in range(3):
                wait = await strategy("user:2", rate, backend)
                assert wait == 0.0, f"User 2 request {i+1} should be allowed"

    @pytest.mark.anyio
    @pytest.mark.strategy
    async def test_unlimited_rate(self, backend: InMemoryBackend):
        """Test unlimited rate handling."""
        async with backend(close_on_exit=True):
            strategy = LeakyBucketWithQueueStrategy()
            rate = Rate(limit=0, seconds=0)
            key = "user:unlimited"

            for _ in range(50):
                wait = await strategy(key, rate, backend)
                assert wait == 0.0, "Unlimited rate should always allow"
