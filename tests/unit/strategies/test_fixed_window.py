"""Tests for Fixed Window strategy."""

import asyncio
import math
from unittest.mock import patch

import pytest

from tests.conftest import BackendGen
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy


@pytest.mark.anyio
@pytest.mark.strategy
class TestFixedWindow:
    async def test_fixed_window_basic_limiting(self, backend: InMemoryBackend):
        """Test basic rate limiting with fixed window strategy."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        key = "user:123"

        # First 3 requests should succeed
        for i in range(3):
            wait = await strategy(key, rate, backend)
            assert wait == 0, f"Request {i + 1} should be allowed"

        # 4th request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Request 4 should be throttled"
        assert wait <= rate.expire, "Wait time should not exceed window duration"

    async def test_fixed_window_reset(self, backend: InMemoryBackend):
        """Test that counter resets after window expires."""

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
        assert wait == 0, "Should be allowed after window reset"

    async def test_fixed_window_different_keys(self, backend: InMemoryBackend):
        """Test that different keys are tracked independently."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("2/s")

        # User 1 uses up their limit
        await strategy("user:1", rate, backend)
        await strategy("user:1", rate, backend)
        wait = await strategy("user:1", rate, backend)
        assert wait > 0, "User 1 should be throttled"

        # User 2 should still be allowed
        wait = await strategy("user:2", rate, backend)
        assert wait == 0, "User 2 should not be affected"

    async def test_fixed_window_unlimited_rate(self, backend: InMemoryBackend):
        """Test that unlimited rates always allow requests."""

        strategy = FixedWindowStrategy()
        rate = Rate(limit=0, seconds=0)
        key = "user:unlimited"

        # Should always be allowed
        for _ in range(100):
            wait = await strategy(key, rate, backend)
            assert wait == 0, "Unlimited rate should always allow requests"

    async def test_fixed_window_concurrent_requests(self, backend: InMemoryBackend):
        """Test strategy under concurrent load."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("10/s")
        key = "user:concurrent"

        # Make 10 concurrent requests
        results = await asyncio.gather(
            *[strategy(key, rate, backend) for _ in range(10)]
        )

        # All 10 should succeed
        allowed = sum(1 for wait in results if wait == 0)
        assert allowed == 10, "All 10 requests should be allowed"

        # 11th request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "11th request should be throttled"

    async def test_fixed_window_boundary_burst(self, backend: InMemoryBackend):
        """Test that fixed window allows bursts at boundaries."""

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
            assert wait == 0, f"Request {i + 1} in new window should be allowed"

    async def test_fixed_window_wait_ms_accuracy(self, backend: InMemoryBackend):
        """Test that wait time calculation is accurate."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("1/200ms")
        key = "user:wait"

        # First request succeeds
        await strategy(key, rate, backend)

        # Second request should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled"
        assert wait <= 200, f"Wait time {wait}ms should not exceed window 200ms"

    async def test_fixed_window_multiple_windows(self, backend: InMemoryBackend):
        """Test behavior across multiple time windows."""

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

    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_fixed_window_sub_second_rates(self, backend: InMemoryBackend):
        """Test with rates smaller than 1 second."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/100ms")
        key = "user:subsecond"

        # Should allow 5 requests
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0, f"Request {i + 1} should be allowed"

        # 6th should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Request 6 should be throttled"
        assert wait <= 100, "Wait should be within window duration"


@pytest.mark.anyio
@pytest.mark.strategy
class TestFixedWindowWholeSecondWindows:
    """
    Regression tests for windows of one second or more.

    Their counter used to live under a single key with a TTL, so the TTL
    *was* the window: it started at the first hit and, because of a 2 second
    floor, a `20/1s` rate actually allowed 20 requests per 2 seconds (a
    request 1.1s after a full burst was throttled), and a 2.5s window was cut
    to 2s. The documented behavior is clock-aligned windows, which is what
    the `wait_ms` they return and `get_stat` already assumed. The counter is
    now keyed by window, so the window ends on the clock boundary and the TTL
    is cleanup only.

    The strategy's wall clock is faked so each test is exact and sleep-free.
    The real backends' TTLs keep running, which is what exposes the old bug:
    a counter that outlives its window.
    """

    async def test_each_window_gets_a_fresh_allowance(self, backends: BackendGen):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        fake_now = [100.2]

        with patch(
            "traffik.strategies.fixed_window.time", side_effect=lambda: fake_now[0]
        ):
            for backend in backends(namespace="fixed_window_whole_second"):
                async with backend(persistent=False, close_on_exit=True):
                    # 100.2s, 101.1s and 102.0s are in three different 1s windows,
                    # each well inside the key's real TTL.
                    for window_time in (100.2, 101.1, 102.0):
                        fake_now[0] = window_time
                        for i in range(3):
                            wait = await strategy("user:fresh", rate, backend)
                            assert wait == 0, (
                                f"{type(backend).__name__}: request {i + 1} at "
                                f"t={window_time} should open a fresh window"
                            )
                        wait = await strategy("user:fresh", rate, backend)
                        assert wait > 0, (
                            f"{type(backend).__name__}: 4th request at "
                            f"t={window_time} should be throttled"
                        )
                    fake_now[0] = 100.2

    async def test_advertised_wait_is_enough(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        fake_now = [100.2]

        with patch(
            "traffik.strategies.fixed_window.time", side_effect=lambda: fake_now[0]
        ):
            for _ in range(3):
                await strategy("user:wait", rate, backend)
            wait = await strategy("user:wait", rate, backend)
            assert wait == pytest.approx(800), "window ends at t=101.0"

            fake_now[0] += wait / 1000
            assert await strategy("user:wait", rate, backend) == 0, (
                "a client that waits the advertised time must be let through"
            )

    @pytest.mark.parametrize("rate_str", ["1/s", "10/m", "3/1500ms", "3/2500ms"])
    async def test_counter_ttl_covers_the_whole_window(
        self, backend: InMemoryBackend, rate_str: str
    ):
        strategy = FixedWindowStrategy()
        rate = Rate.parse(rate_str)

        with patch.object(
            backend, "increment_with_ttl", wraps=backend.increment_with_ttl
        ) as spy:
            await strategy("user:ttl", rate, backend)

        ttl = spy.call_args.kwargs["ttl"]
        assert ttl >= math.ceil(rate.expire / 1000), (
            f"counter TTL {ttl}s expires before the {rate.expire}ms window ends"
        )

    async def test_get_stat_reports_the_current_window(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("3/s")
        fake_now = [100.2]

        with patch(
            "traffik.strategies.fixed_window.time", side_effect=lambda: fake_now[0]
        ):
            for _ in range(2):
                await strategy("user:stat", rate, backend)
            stat = await strategy.get_stat("user:stat", rate, backend)
            assert stat.hits_remaining == 1

            fake_now[0] = 101.1
            stat = await strategy.get_stat("user:stat", rate, backend)
            assert stat.hits_remaining == 3, "a new window starts with a full allowance"
            assert stat.wait_ms == 0
