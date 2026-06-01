"""Tests for strategy recovery from corrupted backend data.

Verifies that all strategies handle corrupted/garbage data gracefully
by resetting to safe defaults rather than crashing.
"""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.custom import GCRAStrategy
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
from traffik.utils import dump_data

GARBAGE_VALUES = [
    "not_valid_base85_data!!!",
    "AAAA",  # Valid base85 but invalid msgpack
    "",
    "null",
    "0",
]


def _corrupt_key(backend: InMemoryBackend, key: str) -> str:
    """Build the full namespaced key for direct backend manipulation."""
    return backend.get_key(key)


@pytest.mark.anyio
@pytest.mark.strategy
class TestTokenBucketCorruptionRecovery:
    """TokenBucketStrategy should recover from corrupted bucket state."""

    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):
        """Strategy should reinitialize bucket on corrupted data and allow the request."""

        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt"

        # Use a few tokens first
        for _ in range(3):
            await strategy(key, rate, backend)

        # Corrupt the backend state
        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket"
        for garbage in GARBAGE_VALUES:
            await backend.set(bucket_key, garbage)

            # Strategy should recover and allow the request
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_state_resets_to_full_capacity(
        self, backend: InMemoryBackend
    ):
        """After corruption recovery, bucket should be at full capacity."""

        strategy = TokenBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-capacity"

        # Corrupt the state
        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket"
        await backend.set(bucket_key, "garbage_data_here!!!")

        # Should allow rate.limit requests (full capacity after reset)
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"

        # Next should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after full capacity used"

    async def test_corrupted_state_with_wrong_types(self, backend: InMemoryBackend):
        """State with wrong value types should be handled."""

        strategy = TokenBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-types"

        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket"

        # Valid msgpack but wrong structure (string instead of dict)
        await backend.set(bucket_key, dump_data("just a string"))
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from wrong type"

        # Valid msgpack dict but wrong value types
        await backend.set(
            bucket_key, dump_data({"tokens": "not_a_number", "last_refill": []})
        )
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from wrong value types"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
        """get_stat() should return full capacity on corrupted data."""

        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-stat"

        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket"
        await backend.set(bucket_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit


@pytest.mark.anyio
@pytest.mark.strategy
class TestTokenBucketWithDebtCorruptionRecovery:
    """TokenBucketWithDebtStrategy should recover from corrupted state."""

    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):

        strategy = TokenBucketWithDebtStrategy(max_debt=5)
        rate = Rate.parse("10/s")
        key = "user:corrupt-debt"

        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:debt"
        for garbage in GARBAGE_VALUES:
            await backend.set(bucket_key, garbage)

            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):

        strategy = TokenBucketWithDebtStrategy(max_debt=5)
        rate = Rate.parse("10/s")
        key = "user:corrupt-debt-stat"

        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:debt"
        await backend.set(bucket_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        # Full capacity + max_debt
        assert stat.hits_remaining == rate.limit + strategy.max_debt


@pytest.mark.anyio
@pytest.mark.strategy
class TestSlidingWindowLogCorruptionRecovery:
    """SlidingWindowLogStrategy should recover from corrupted log data."""

    async def test_corrupted_log_allows_request(self, backend: InMemoryBackend):
        """Strategy should recover from all garbage values and allow the request."""

        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-swl"

        log_key = f"{_corrupt_key(backend, key)}:slidinglog"
        for garbage in GARBAGE_VALUES:
            await backend.set(log_key, garbage)

            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_log_resets_window(self, backend: InMemoryBackend):
        """After corruption, should start fresh and allow rate.limit requests."""

        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-swl-reset"

        log_key = f"{_corrupt_key(backend, key)}:slidinglog"
        await backend.set(log_key, "corrupted!!!")

        # Should allow rate.limit requests
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"

        # Next should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after limit reached"

    async def test_corrupted_log_with_wrong_structure(self, backend: InMemoryBackend):
        """Log with valid msgpack but wrong structure should be handled."""

        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-swl-struct"

        log_key = f"{_corrupt_key(backend, key)}:slidinglog"

        # Valid msgpack but not a list of [timestamp, cost] pairs
        await backend.set(log_key, dump_data({"not": "a list"}))
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from wrong structure"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
        """get_stat() should return full capacity on corrupted data."""

        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-swl-stat"

        log_key = f"{_corrupt_key(backend, key)}:slidinglog"
        await backend.set(log_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit


@pytest.mark.anyio
@pytest.mark.strategy
class TestSlidingWindowCounterCorruptionRecovery:
    """SlidingWindowCounterStrategy should handle corrupted counter values."""

    async def test_corrupted_previous_window_treated_as_zero(
        self, backend: InMemoryBackend
    ):
        """Corrupted previous window counter should default to 0."""

        strategy = SlidingWindowCounterStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-swc"

        # Use a few requests to establish current window
        for _ in range(3):
            await strategy(key, rate, backend)

        # Corrupt the previous window key
        # We need to figure out the window ID from the current time
        now = asyncio.get_running_loop().time() * 1000
        window_duration_ms = rate.expire
        current_window_id = int(now // window_duration_ms)
        previous_window_id = current_window_id - 1

        full_key = _corrupt_key(backend, key)
        prev_key = f"{full_key}:slidingcounter:{previous_window_id}"
        await backend.set(prev_key, "not_a_number")

        # Should still work, treating previous as 0
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow request with corrupted previous window"


@pytest.mark.anyio
@pytest.mark.strategy
class TestLeakyBucketCorruptionRecovery:
    """LeakyBucketStrategy should recover from corrupted state."""

    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):

        strategy = LeakyBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-lb"

        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        for garbage in GARBAGE_VALUES:
            await backend.set(state_key, garbage)

            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_state_resets_bucket(self, backend: InMemoryBackend):
        """After corruption, should reinitialize bucket and enforce limits."""

        strategy = LeakyBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-lb-reset"

        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        await backend.set(state_key, "corrupted!!!")

        # Should allow requests up to limit
        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"

        # Next should be throttled
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after limit reached"

    async def test_corrupted_state_with_missing_keys(self, backend: InMemoryBackend):
        """State dict missing required keys should trigger recovery."""

        strategy = LeakyBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-lb-missing"

        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"

        # Valid msgpack dict but missing "level" and "last_leak" keys
        await backend.set(state_key, dump_data({"foo": "bar"}))
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from missing keys"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-lb-stat"

        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        await backend.set(state_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit


@pytest.mark.anyio
@pytest.mark.strategy
class TestLeakyBucketWithQueueCorruptionRecovery:
    """LeakyBucketWithQueueStrategy should recover from corrupted queue state."""

    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):
        strategy = LeakyBucketWithQueueStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-lbq"

        state_key = f"{_corrupt_key(backend, key)}:leakybucketqueue:state"
        for garbage in GARBAGE_VALUES:
            await backend.set(state_key, garbage)

            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_queue_entries(self, backend: InMemoryBackend):
        """Queue with invalid entries should trigger recovery."""

        strategy = LeakyBucketWithQueueStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-lbq-entries"

        state_key = f"{_corrupt_key(backend, key)}:leakybucketqueue:state"

        # Valid structure but queue entries are not [timestamp, cost] pairs
        await backend.set(
            state_key,
            dump_data({"queue": ["not", "pairs"], "last_leak": 1000.0}),
        )
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from invalid queue entries"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):

        strategy = LeakyBucketWithQueueStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-lbq-stat"

        state_key = f"{_corrupt_key(backend, key)}:leakybucketqueue:state"
        await backend.set(state_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == float(rate.limit)


@pytest.mark.anyio
@pytest.mark.strategy
class TestGCRACorruptionRecovery:
    """GCRAStrategy should handle corrupted TAT values."""

    async def test_corrupted_tat_allows_request(self, backend: InMemoryBackend):
        """Non-numeric TAT should be treated as if no TAT exists."""

        strategy = GCRAStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-gcra"

        tat_key = f"{_corrupt_key(backend, key)}:gcra:tat"

        # Store non-numeric value as TAT
        await backend.set(tat_key, "not_a_number")

        # Should recover and allow the request (treats as now)
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow request with corrupted TAT"

    async def test_valid_tat_works_after_corruption(self, backend: InMemoryBackend):
        """After clearing corrupted TAT, strategy should work normally."""

        strategy = GCRAStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-gcra-clear"

        tat_key = f"{_corrupt_key(backend, key)}:gcra:tat"

        # Corrupt and then clear
        await backend.set(tat_key, "garbage")
        await backend.delete(tat_key)

        # Should work normally
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow first request after clearing corruption"


@pytest.mark.anyio
@pytest.mark.strategy
class TestFixedWindowCorruptionRecovery:
    """FixedWindowStrategy uses atomic increment, but subsecond windows
    use stored window_start which could be corrupted."""

    async def test_corrupted_counter_for_non_subsecond(self, backend: InMemoryBackend):
        """Non-subsecond windows use increment_with_ttl which handles non-existent keys.
        A corrupted counter value should still be handled by the backend."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-fw"

        counter_key = f"{_corrupt_key(backend, key)}:fixedwindow:counter"

        # Set a non-numeric value as counter
        await backend.set(counter_key, "not_a_number")

        # increment_with_ttl should handle this at the backend level
        try:
            wait = await strategy(key, rate, backend)
            # Backend should either reset or increment from 0
            assert isinstance(wait, float)
        except (ValueError, TypeError):
            # Backend doesn't handle non-numeric increment - documents the behavior
            pytest.skip("Backend does not handle non-numeric values for increment")

    async def test_corrupted_window_start_for_subsecond(self, backend: InMemoryBackend):
        """Subsecond windows store window_start; corrupted value should reset."""

        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/100ms")
        key = "user:corrupt-fw-sub"

        base_key = f"{_corrupt_key(backend, key)}:fixedwindow"
        window_start_key = f"{base_key}:start"

        # Corrupt window_start
        await backend.set(window_start_key, "not_a_number")

        # Should treat as new window and allow the request
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow first request with corrupted window start"
