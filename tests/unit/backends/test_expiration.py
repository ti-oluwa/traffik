"""Tests for bounded random-sampling active expiration in in-memory backends."""

import asyncio
import multiprocessing
import platform

import pytest

from traffik.backends.inmemory import InMemoryBackend

MAYBE_POSIX = platform.system() != "Windows"
SUPPORTS_FORK = MAYBE_POSIX and "fork" in multiprocessing.get_all_start_methods()


@pytest.mark.anyio
class TestInMemoryBackendExpiration:
    async def test_cleanup_reclaims_expired_keys(self):
        """`_cleanup()` removes expired keys via sampling, without a full scan."""
        backend = InMemoryBackend(
            cleanup_frequency=None, cleanup_sample_size=10, number_of_shards=2
        )
        async with backend(close_on_exit=True):
            for i in range(100):
                await backend.set(f"k{i}", "v", expire=0.01)
            await asyncio.sleep(0.05)

            for _ in range(20):  # several rounds to reclaim across shards
                await backend._cleanup()

            remaining = 0
            for i in range(100):
                if await backend.get(f"k{i}") is not None:
                    remaining += 1
            assert remaining == 0

    async def test_cleanup_skips_non_expiring_keys(self):
        """Keys set without a TTL are never reclaimed by cleanup."""
        backend = InMemoryBackend(cleanup_frequency=None, number_of_shards=1)
        async with backend(close_on_exit=True):
            await backend.set("persistent", "v")
            for _ in range(10):
                await backend._cleanup()
            assert await backend.get("persistent") == "v"

    async def test_background_cleanup_task_reclaims_over_time(self):
        """The `cleanup_frequency` background task actively reclaims expired keys."""
        backend = InMemoryBackend(
            cleanup_frequency=0.02, cleanup_sample_size=10, number_of_shards=2
        )
        async with backend(close_on_exit=True):
            for i in range(50):
                await backend.set(f"k{i}", "v", expire=0.01)
            await asyncio.sleep(0.5)

            remaining_indexed = sum(len(lst) for lst in backend._shard_key_lists)
            assert remaining_indexed == 0

    async def test_index_stays_consistent_under_churn(self):
        """The sampling index matches shard contents after mixed operations."""
        backend = InMemoryBackend(
            cleanup_frequency=0.01, cleanup_sample_size=5, number_of_shards=3
        )
        async with backend(close_on_exit=True):
            for round_ in range(10):
                for i in range(50):
                    ttl = 0.005 if i % 2 else None
                    await backend.set(f"k{round_}-{i}", "v", expire=ttl)
                await backend.increment(f"counter-{round_}")
                await backend.delete(f"k{round_}-0")
                await asyncio.sleep(0.02)

            for shard_idx, shard in enumerate(backend._shards):
                key_list = backend._shard_key_lists[shard_idx]
                positions = backend._shard_key_positions[shard_idx]
                assert len(key_list) == len(positions) == len(shard)
                assert set(key_list) == set(shard.keys())
                for key, pos in positions.items():
                    assert key_list[pos] == key

    async def test_increment_value_still_returned_as_string(self):
        """Internal raw-int storage for counters doesn't change get()'s contract."""
        backend = InMemoryBackend(cleanup_frequency=None)
        async with backend(close_on_exit=True):
            await backend.increment("counter", 5)
            await backend.increment_with_ttl("counter_ttl", 3, ttl=60)

            assert await backend.get("counter") == "5"
            assert await backend.get("counter_ttl") == "3"
            values = await backend.multi_get("counter", "counter_ttl")
            assert values == ["5", "3"]
            assert all(isinstance(v, str) for v in values)


@pytest.mark.anyio
@pytest.mark.skipif(not SUPPORTS_FORK, reason="Requires POSIX fork support")
class TestMultiProcessInMemoryBackendExpiration:
    async def test_cleanup_reclaims_expired_slots(self):
        """`_cleanup()` reclaims expired slots via bucket sampling."""
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="test-mp-expiry-reclaim",
            max_keys=512,
            number_of_shards=2,
            cleanup_frequency=None,
            cleanup_sample_size=20,
        )
        async with backend(close_on_exit=True):
            for i in range(100):
                await backend.set(f"k{i}", "v", expire=0.01)
            await asyncio.sleep(0.05)

            for _ in range(20):
                backend._cleanup()

            remaining = 0
            for i in range(100):
                if await backend.get(f"k{i}") is not None:
                    remaining += 1
            assert remaining == 0

    async def test_cleanup_skips_non_expiring_slots(self):
        """Keys set without a TTL are never reclaimed by cleanup."""
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="test-mp-expiry-persist",
            max_keys=64,
            number_of_shards=1,
            cleanup_frequency=None,
        )
        async with backend(close_on_exit=True):
            await backend.set("persistent", "v")
            for _ in range(10):
                backend._cleanup()
            assert await backend.get("persistent") == "v"

    async def test_background_cleanup_task_reclaims_over_time(self):
        """The `cleanup_frequency` background task actively reclaims expired slots."""
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="test-mp-expiry-bg",
            max_keys=512,
            number_of_shards=2,
            cleanup_frequency=0.02,
            cleanup_sample_size=20,
        )
        async with backend(close_on_exit=True):
            for i in range(100):
                await backend.set(f"k{i}", "v", expire=0.01)
            await asyncio.sleep(0.5)

            buffer = backend._buffer
            assert buffer is not None
            occupied = 0
            for shard_idx in range(2):
                shard_base = backend._shard_base(shard_idx)
                occupied += sum(
                    1 for _ in backend._hash_table_iter_occupied(buffer, shard_base)
                )
            # Active sampling won't necessarily reclaim every slot, but should
            # make substantial progress over many background cleanup ticks.
            assert occupied < 50
