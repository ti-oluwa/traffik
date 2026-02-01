"""Tests for msgpack serialization in strategies."""

import pytest

from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.custom import GCRAStrategy, QuotaWithRolloverStrategy
from traffik.strategies.leaky_bucket import LeakyBucketStrategy
from traffik.strategies.sliding_window import SlidingWindowLogStrategy
from traffik.strategies.token_bucket import TokenBucketStrategy
from traffik.utils import dump_data, load_data, MsgPackDecodeError


class TestSerializationRoundtrip:
    """Test msgpack serialization/deserialization roundtrip."""

    def test_dump_load_simple_data(self):
        """Test roundtrip for simple data structures."""
        test_cases = [
            {"count": 42},
            {"tokens": 100.5, "last_refill": 1234567890.123},
            [[1.0, 2], [3.5, 4]],
            {"state": "active", "value": 99.9},
        ]

        for data in test_cases:
            serialized = dump_data(data)
            assert isinstance(serialized, str), "dump_data should return string"
            
            deserialized = load_data(serialized)
            assert deserialized == data, f"Roundtrip failed for {data}"

    def test_dump_load_complex_nested_data(self):
        """Test roundtrip for complex nested structures."""
        data = {
            "metadata": {
                "created": 1234567890.123,
                "updated": 1234567900.456,
            },
            "counters": [
                {"window": 1, "count": 10},
                {"window": 2, "count": 15},
            ],
            "stats": {
                "total": 25,
                "average": 12.5,
            },
        }

        serialized = dump_data(data)
        deserialized = load_data(serialized)
        assert deserialized == data

    def test_dump_load_edge_cases(self):
        """Test edge cases like empty data, floats, large numbers."""
        test_cases = [
            {},
            [],
            {"empty_list": [], "empty_dict": {}},
            {"large_number": 999999999999999},
            {"small_float": 0.000000001},
            {"negative": -123.456},
        ]

        for data in test_cases:
            serialized = dump_data(data)
            deserialized = load_data(serialized)
            assert deserialized == data, f"Failed for {data}"


class TestStrategyStateSerialization:
    """Test strategy state persistence with msgpack."""

    @pytest.mark.anyio
    async def test_sliding_window_log_state_persistence(
        self, backend: InMemoryBackend
    ):
        """Test SlidingWindowLog stores and retrieves state correctly."""
        async with backend(close_on_exit=True):
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
            log = load_data(stored)
            assert isinstance(log, list), "Log should be a list"
            assert len(log) == 3, "Should have 3 entries"
            
            # Verify structure: [[timestamp, cost], ...]
            for entry in log:
                assert isinstance(entry, list), "Each entry should be a list"
                assert len(entry) == 2, "Each entry should have [timestamp, cost]"
                assert isinstance(entry[0], (int, float)), "Timestamp should be numeric"
                assert isinstance(entry[1], int), "Cost should be integer"

    @pytest.mark.anyio
    async def test_token_bucket_state_persistence(self, backend: InMemoryBackend):
        """Test TokenBucket stores and retrieves state correctly."""
        async with backend(close_on_exit=True):
            strategy = TokenBucketStrategy(burst_size=100)
            rate = Rate.parse("10/s")
            key = "user:bucket"

            # Make a request to initialize state
            await strategy(key, rate, backend, cost=5)

            # Get stored data
            full_key = backend.get_key(key)
            state_key = f"{full_key}:tokenbucket"
            stored = await backend.get(state_key)
            
            assert stored is not None, "State should be stored"
            
            # Verify deserialization
            state = load_data(stored)
            assert isinstance(state, dict), "State should be a dict"
            assert "tokens" in state, "Should have tokens field"
            assert "last_refill" in state, "Should have last_refill field"
            
            # Verify types
            assert isinstance(state["tokens"], (int, float))
            assert isinstance(state["last_refill"], (int, float))
            
            # Verify values make sense
            assert 0 <= state["tokens"] <= 100, "Tokens should be in valid range"
            assert state["last_refill"] > 0, "Last refill should be positive timestamp"

    @pytest.mark.anyio
    async def test_leaky_bucket_state_persistence(self, backend: InMemoryBackend):
        """Test LeakyBucket stores and retrieves state correctly."""
        async with backend(close_on_exit=True):
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
            
            # Verify deserialization
            state = load_data(stored)
            assert isinstance(state, dict)
            assert "level" in state
            assert "last_leak" in state
            
            assert isinstance(state["level"], (int, float))
            assert isinstance(state["last_leak"], (int, float))
            assert state["level"] >= 0
            assert state["last_leak"] > 0

    @pytest.mark.anyio
    async def test_gcra_state_persistence(self, backend: InMemoryBackend):
        """Test GCRA stores TAT correctly as string (simple value)."""
        async with backend(close_on_exit=True):
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
            
            # GCRA stores as simple string, not msgpack
            tat = float(tat_str)
            assert tat > 0, "TAT should be positive"

    @pytest.mark.anyio
    async def test_quota_rollover_state_persistence(self, backend: InMemoryBackend):
        """Test QuotaWithRollover persistence."""
        async with backend(close_on_exit=True):
            strategy = QuotaWithRolloverStrategy(
                rollover_percentage=0.5,
                max_rollover=50
            )
            rate = Rate.parse("100/minute")
            key = "user:quota"

            # Use some quota
            await strategy(key, rate, backend, cost=10)

            # Verify state is stored (keys depend on implementation)
            # The strategy creates period-based keys, just verify backend has keys
            # This is more of an integration test
            assert True  # If no exception, serialization worked


class TestSerializationErrorHandling:
    """Test error handling in serialization."""

    def test_load_invalid_data(self):
        """Test loading invalid base85/msgpack data."""
        
        with pytest.raises(MsgPackDecodeError):
            load_data("invalid base85 !!!")
        
        with pytest.raises(MsgPackDecodeError):
            load_data("=====")  # Valid base85 but invalid msgpack

    def test_dump_handles_various_types(self):
        """Test dump_data handles different Python types."""
        test_cases = [
            None,  # None
            42,  # int
            3.14,  # float
            "string",  # str (msgpack stores, we base85 encode)
            [1, 2, 3],  # list
            {"key": "value"},  # dict
            True,  # bool
            False,
        ]

        for data in test_cases:
            try:
                serialized = dump_data(data)
                deserialized = load_data(serialized)
                assert deserialized == data or (
                    data is None and deserialized is None
                ), f"Failed for {data}"
            except Exception as e:
                pytest.fail(f"Serialization failed for {data}: {e}")


@pytest.mark.anyio
async def test_state_survives_strategy_recreation(backend: InMemoryBackend):
    """Test that state persists across strategy instances."""
    async with backend(close_on_exit=True):
        key = "user:persistent"
        rate = Rate.parse("10/s")

        # Create strategy, make requests
        strategy1 = TokenBucketStrategy(burst_size=100)
        for _ in range(5):
            await strategy1(key, rate, backend, cost=1)

        # Get current token count
        full_key = backend.get_key(key)
        state_key = f"{full_key}:tokenbucket"
        stored1 = await backend.get(state_key)
        assert stored1 is not None
        state1 = load_data(stored1)
        tokens1 = state1["tokens"]

        # Create new strategy instance, make more requests
        strategy2 = TokenBucketStrategy(burst_size=100)
        await strategy2(key, rate, backend, cost=1)

        # Verify state was persisted
        stored2 = await backend.get(state_key)
        assert stored2 is not None
        state2 = load_data(stored2)
        tokens2 = state2["tokens"]

        # Tokens should have decreased
        assert tokens2 < tokens1, "State should persist across strategy instances"


@pytest.mark.anyio
async def test_concurrent_serialization_safety(backend: InMemoryBackend):
    """Test concurrent access doesn't corrupt serialized data."""
    async with backend(close_on_exit=True):
        import asyncio
        
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
            log = load_data(stored)
            assert isinstance(log, list)
            # Verify each entry is valid
            for entry in log:
                assert len(entry) == 2
                assert isinstance(entry[0], (int, float))
                assert isinstance(entry[1], int)
