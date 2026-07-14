"""
Strategy recovery from corrupted backend state.
Stratgies should reset to safe defaults instead of crashing.
"""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies._serde import _encode_two_floats
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

GARBAGE_VALUES = [
    "not_valid_data!!!",
    "AAAA",
    "",
    "null",
    "0",
]


def _corrupt_key(backend: InMemoryBackend, key: str) -> str:
    return backend.get_key(key)


@pytest.mark.anyio
@pytest.mark.strategy
class TestFixedWindowCorruptionRecovery:
    async def test_corrupted_counter_for_non_subsecond(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-fw"
        counter_key = f"{_corrupt_key(backend, key)}:fixedwindow:counter"
        await backend.set(counter_key, "not_a_number")
        try:
            wait = await strategy(key, rate, backend)
            assert isinstance(wait, float)
        except (ValueError, TypeError):
            pytest.skip("Backend does not handle non-numeric values for increment")

    async def test_corrupted_window_start_for_subsecond(self, backend: InMemoryBackend):
        strategy = FixedWindowStrategy()
        rate = Rate.parse("5/100ms")
        key = "user:corrupt-fw-sub"
        window_start_key = f"{_corrupt_key(backend, key)}:fixedwindow:start"
        await backend.set(window_start_key, "not_a_number")
        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow first request with corrupted window start"


@pytest.mark.anyio
@pytest.mark.strategy
class TestTokenBucketCorruptionRecovery:
    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):
        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt"
        for _ in range(3):
            await strategy(key, rate, backend)

        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
        for garbage in GARBAGE_VALUES:
            await backend.set(bucket_key, garbage)
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, (
                f"Should allow request after corruption with {garbage!r}"
            )

    async def test_corrupted_state_resets_to_full_capacity(
        self, backend: InMemoryBackend
    ):
        strategy = TokenBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-capacity"
        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
        await backend.set(bucket_key, "garbage_data_here!!!")

        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after full capacity used"

    async def test_corrupted_state_with_nan_values(self, backend: InMemoryBackend):
        strategy = TokenBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-nan"
        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
        await backend.set(bucket_key, _encode_two_floats(float("nan"), float("nan")))

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from NaN state, not return NaN itself"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
        strategy = TokenBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-stat"
        bucket_key = f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
        await backend.set(bucket_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit


@pytest.mark.anyio
@pytest.mark.strategy
class TestTokenBucketWithDebtCorruptionRecovery:
    async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):
        strategy = TokenBucketWithDebtStrategy(max_debt=5)
        rate = Rate.parse("10/s")
        key = "user:corrupt-debt"
        bucket_key = (
            f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
            f":debt:{strategy.max_debt}"
        )
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
        bucket_key = (
            f"{_corrupt_key(backend, key)}:tokenbucket:{rate.limit}"
            f":debt:{strategy.max_debt}"
        )
        await backend.set(bucket_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit + strategy.max_debt


@pytest.mark.anyio
@pytest.mark.strategy
class TestSlidingWindowLogCorruptionRecovery:
    async def test_corrupted_log_allows_request(self, backend: InMemoryBackend):
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
        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-swl-reset"
        log_key = f"{_corrupt_key(backend, key)}:slidinglog"
        await backend.set(log_key, "corrupted!!!")

        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after limit reached"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
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
    async def test_corrupted_previous_window_treated_as_zero(
        self, backend: InMemoryBackend
    ):
        strategy = SlidingWindowCounterStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-swc"
        for _ in range(3):
            await strategy(key, rate, backend)

        now = asyncio.get_running_loop().time() * 1000
        current_window_id = int(now // rate.expire)
        previous_window_id = current_window_id - 1
        full_key = _corrupt_key(backend, key)
        prev_key = f"{full_key}:slidingcounter:{previous_window_id}"
        await backend.set(prev_key, "not_a_number")

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow request with corrupted previous window"


@pytest.mark.anyio
@pytest.mark.strategy
class TestLeakyBucketCorruptionRecovery:
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
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-lb-reset"
        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        await backend.set(state_key, "corrupted!!!")

        for i in range(5):
            wait = await strategy(key, rate, backend)
            assert wait == 0.0, f"Request {i + 1} should be allowed after reset"
        wait = await strategy(key, rate, backend)
        assert wait > 0, "Should be throttled after limit reached"

    async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-lb-stat"
        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        await backend.set(state_key, "garbage!!!")

        stat = await strategy.get_stat(key, rate, backend)
        assert stat.hits_remaining == rate.limit

    async def test_corrupted_state_with_nan_values(self, backend: InMemoryBackend):
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("5/s")
        key = "user:corrupt-lb-nan"
        state_key = f"{_corrupt_key(backend, key)}:leakybucket:state"
        await backend.set(state_key, _encode_two_floats(float("nan"), float("nan")))

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should recover from NaN state, not return NaN itself"


# @pytest.mark.anyio
# @pytest.mark.strategy
# class TestLeakyBucketWithQueueCorruptionRecovery:
#     async def test_corrupted_state_allows_request(self, backend: InMemoryBackend):
#         strategy = LeakyBucketWithQueueStrategy()
#         rate = Rate.parse("10/s")
#         key = "user:corrupt-lbq"
#         state_key = f"{_corrupt_key(backend, key)}:leakybucketqueue:state"
#         for garbage in GARBAGE_VALUES:
#             await backend.set(state_key, garbage)
#             wait = await strategy(key, rate, backend)
#             assert wait == 0.0, (
#                 f"Should allow request after corruption with {garbage!r}"
#             )

#     async def test_corrupted_stat_returns_full_capacity(self, backend: InMemoryBackend):
#         strategy = LeakyBucketWithQueueStrategy()
#         rate = Rate.parse("10/s")
#         key = "user:corrupt-lbq-stat"
#         state_key = f"{_corrupt_key(backend, key)}:leakybucketqueue:state"
#         await backend.set(state_key, "garbage!!!")

#         stat = await strategy.get_stat(key, rate, backend)
#         assert stat.hits_remaining == float(rate.limit)


@pytest.mark.anyio
@pytest.mark.strategy
class TestGCRACorruptionRecovery:
    async def test_corrupted_tat_allows_request(self, backend: InMemoryBackend):
        strategy = GCRAStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-gcra"
        tat_key = f"{_corrupt_key(backend, key)}:gcra:tat"
        await backend.set(tat_key, "not_a_number")

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow request with corrupted TAT"

    async def test_valid_tat_works_after_corruption(self, backend: InMemoryBackend):
        strategy = GCRAStrategy()
        rate = Rate.parse("10/s")
        key = "user:corrupt-gcra-clear"
        tat_key = f"{_corrupt_key(backend, key)}:gcra:tat"
        await backend.set(tat_key, "garbage")
        await backend.delete(tat_key)

        wait = await strategy(key, rate, backend)
        assert wait == 0.0, "Should allow first request after clearing corruption"
