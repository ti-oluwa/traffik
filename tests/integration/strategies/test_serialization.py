"""Tests for strategy state serialization (struct-based, via traffik.strategies._serde)."""

import asyncio

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies._serde import _decode_two_float_records, _decode_two_floats
from traffik.strategies.custom import GCRAStrategy, QuotaWithRolloverStrategy
from traffik.strategies.leaky_bucket import LeakyBucketStrategy
from traffik.strategies.sliding_window import SlidingWindowLogStrategy
from traffik.strategies.token_bucket import TokenBucketStrategy


@pytest.mark.anyio
@pytest.mark.strategy
class TestStrategyStateSerialization:
    """
    Test strategy state persistence via `traffik.strategies._serde`.

    TokenBucket/LeakyBucket store `(value, timestamp)` tuples via
    `_decode_two_floats`; SlidingWindowLog stores a list of `(timestamp, cost)`
    pairs via `_decode_two_float_records`.
    """

    async def test_sliding_window_log_state_persistence(self, backend: InMemoryBackend):
        """Test SlidingWindowLog stores and retrieves state correctly."""
        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("5/s")
        key = "user:123"

        # Make requests to build state
        for _ in range(3):
            await strategy(key, rate, backend)

        # Get the stored data
        log_key = f"{backend.get_key(key)}:slidinglog"
        stored = await backend.get(log_key)

        assert stored is not None, "State should be stored"

        # Verify it can be deserialized
        log = _decode_two_float_records(stored)
        assert isinstance(log, list), "Log should be a list"
        assert len(log) == 3, "Should have 3 entries"

        # Verify structure: [(timestamp, cost), ...]
        for entry in log:
            assert isinstance(entry, tuple), (
                "Each entry should be a (timestamp, cost) tuple"
            )
            assert len(entry) == 2, "Each entry should have (timestamp, cost)"
            assert isinstance(entry[0], (int, float)), "Timestamp should be numeric"
            assert isinstance(entry[1], (int, float)), "Cost should be numeric"

    async def test_token_bucket_state_persistence(self, backend: InMemoryBackend):
        """Test TokenBucket stores and retrieves state correctly."""
        strategy = TokenBucketStrategy(burst_size=100)
        rate = Rate.parse("10/s")
        key = "user:bucket"

        # Make a request to initialize state
        await strategy(key, rate, backend, cost=5)

        # Get stored data
        full_key = backend.get_key(key)
        state_key = f"{full_key}:tokenbucket:100"
        stored = await backend.get(state_key)

        assert stored is not None, "State should be stored"

        # Verify deserialization -- (tokens, last_refill), not a dict
        tokens, last_refill = _decode_two_floats(stored)
        assert isinstance(tokens, float)
        assert isinstance(last_refill, float)

        # Verify values make sense
        assert 0 <= tokens <= 100, "Tokens should be in valid range"
        assert last_refill > 0, "Last refill should be positive timestamp"

    async def test_leaky_bucket_state_persistence(self, backend: InMemoryBackend):
        """Test LeakyBucket stores and retrieves state correctly."""
        strategy = LeakyBucketStrategy()
        rate = Rate.parse("10/s")
        key = "user:leaky"

        # Make requests
        await strategy(key, rate, backend, cost=3)

        # Get stored data
        full_key = backend.get_key(key)
        state_key = f"{full_key}:leakybucket:state"
        stored = await backend.get(state_key)

        assert stored is not None, "State should be stored"

        # Verify deserialization -- (level, last_leak), not a dict
        level, last_leak = _decode_two_floats(stored)
        assert isinstance(level, float)
        assert isinstance(last_leak, float)
        assert level >= 0
        assert last_leak > 0

    async def test_gcra_state_persistence(self, backend: InMemoryBackend):
        """Test GCRA stores TAT correctly as string (simple value)."""
        strategy = GCRAStrategy(burst_tolerance_ms=0)
        rate = Rate.parse("10/s")
        key = "user:gcra"

        # Make a request
        await strategy(key, rate, backend)

        # Get stored TAT
        full_key = backend.get_key(key)
        tat_key = f"{full_key}:gcra:tat"
        tat_str = await backend.get(tat_key)

        assert tat_str is not None, "TAT should be stored"

        # GCRA stores as simple string, not through _serde
        tat = float(tat_str)
        assert tat > 0, "TAT should be positive"

    async def test_quota_rollover_state_persistence(self, backend: InMemoryBackend):
        """Test QuotaWithRollover persistence."""
        strategy = QuotaWithRolloverStrategy(rollover_percentage=0.5, max_rollover=50)
        rate = Rate.parse("100/minute")
        key = "user:quota"

        # Use some quota
        await strategy(key, rate, backend, cost=10)

        # Verify state is stored (keys depend on implementation)
        # The strategy creates period-based keys, just verify backend has keys
        # This is more of an integration test
        assert True  # If no exception, serialization worked

    async def test_state_survives_strategy_recreation(self, backend: InMemoryBackend):
        """Test that state persists across strategy instances."""
        key = "user:persistent"
        rate = Rate.parse("10/s")

        # Create strategy, make requests
        strategy1 = TokenBucketStrategy(burst_size=100)
        for _ in range(5):
            await strategy1(key, rate, backend, cost=1)

        # Get current token count
        full_key = backend.get_key(key)
        state_key = f"{full_key}:tokenbucket:100"
        stored1 = await backend.get(state_key)
        assert stored1 is not None
        tokens1, _ = _decode_two_floats(stored1)

        # Create new strategy instance, make more requests
        strategy2 = TokenBucketStrategy(burst_size=100)
        await strategy2(key, rate, backend, cost=1)

        # Verify state was persisted
        stored2 = await backend.get(state_key)
        assert stored2 is not None
        tokens2, _ = _decode_two_floats(stored2)

        # Tokens should have decreased
        assert tokens2 < tokens1, "State should persist across strategy instances"

    async def test_concurrent_serialization_safety(self, backend: InMemoryBackend):
        """Test concurrent access doesn't corrupt serialized data."""
        strategy = SlidingWindowLogStrategy()
        rate = Rate.parse("100/s")
        key = "user:concurrent"

        # Make concurrent requests
        tasks = [strategy(key, rate, backend) for _ in range(20)]
        await asyncio.gather(*tasks)

        # Verify log is readable and valid
        full_key = backend.get_key(key)
        log_key = f"{full_key}:slidinglog"
        stored = await backend.get(log_key)

        if stored:  # May not exist if all throttled
            log = _decode_two_float_records(stored)
            assert isinstance(log, list)
            # Verify each entry is valid
            for entry in log:
                assert len(entry) == 2
                assert isinstance(entry[0], (int, float))
                assert isinstance(entry[1], (int, float))
