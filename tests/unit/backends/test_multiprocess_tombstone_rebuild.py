"""
Tests for tombstone reclamation in `MultiProcessInMemoryBackend`.

Linear probing means a deleted key leaves a tombstone rather than a
genuinely empty bucket. Left unchecked, tombstones accumulate under key
churn (TTL expiry, `delete`, repeated `clear`) and probe chains grow over
time, degrading lookups toward O(shard capacity) even while the live key
count stays flat. The backend tracks a per-shard tombstone counter and
rebuilds that shard's hash table in place once the counter crosses
`tombstone_rebuild_threshold * shard_hash_table_capacity`.
"""

import multiprocessing
import platform

import pytest

SUPPORTS_FORK = (
    platform.system() != "Windows" and "fork" in multiprocessing.get_all_start_methods()
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.backend,
    pytest.mark.skipif(
        not SUPPORTS_FORK,
        reason="`MultiProcessInMemoryBackend` requires the 'fork' start method.",
    ),
]


@pytest.fixture
async def mp_backend():
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(
        namespace="tombstone_test",
        number_of_shards=1,
        max_keys=16,
        tombstone_rebuild_threshold=0.2,
    )
    async with backend(close_on_exit=True):
        yield backend


class TestTombstoneRebuildThreshold:
    def test_rejects_threshold_at_or_above_headroom(self):
        from traffik.backends.multiprocess import (
            _HASH_TABLE_LOAD_FACTOR,
            MultiProcessInMemoryBackend,
        )

        headroom = 1 - _HASH_TABLE_LOAD_FACTOR
        for bad in (headroom, headroom + 0.1, 0.0, -0.1, 1.0):
            with pytest.raises(ValueError):
                MultiProcessInMemoryBackend(tombstone_rebuild_threshold=bad)

    def test_accepts_threshold_within_headroom(self):
        from traffik.backends.multiprocess import (
            _HASH_TABLE_LOAD_FACTOR,
            MultiProcessInMemoryBackend,
        )

        backend = MultiProcessInMemoryBackend(
            tombstone_rebuild_threshold=(1 - _HASH_TABLE_LOAD_FACTOR) / 2
        )
        assert backend._tombstone_rebuild_threshold_count >= 1


class TestTombstoneReclamation:
    async def test_tombstone_count_resets_once_threshold_is_crossed(
        self, mp_backend
    ) -> None:
        """
        Deleting keys past the configured threshold must trigger a rebuild,
        evidenced by the tombstone counter dropping back down instead of
        growing without bound.
        """
        backend = mp_backend
        shard_base = 0
        threshold = backend._tombstone_rebuild_threshold_count

        counts = []
        for i in range(threshold * 5):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)
            counts.append(backend._read_tombstone_count(backend._buffer, shard_base))

        # Proves the counter is actually being bumped at all - a stubbed-out
        # or otherwise disabled bump would leave every reading at 0, which
        # would also (trivially, wrongly) satisfy a bare "it hit 0" check.
        assert max(counts) >= 1, "tombstone counter was never incremented"
        # Never observed above the threshold: `_hash_table_delete` rebuilds
        # (resetting to 0) in the same call that pushes it to the threshold.
        assert max(counts) <= threshold
        # Went back down at least once - i.e. an actual reset happened,
        # not just a monotonic climb that coincidentally never got checked
        # past the threshold.
        resets = sum(1 for prev, curr in zip(counts, counts[1:]) if curr < prev)
        assert resets >= 1, "tombstone counter climbed but never reset"

    async def test_heavy_churn_does_not_exhaust_the_shard_without_rebuild(
        self, mp_backend
    ) -> None:
        """
        This is the concrete failure mode tombstone reclamation exists to
        prevent: with linear probing, a lookup for an absent key only
        terminates when it hits a genuinely empty bucket (or scans the
        whole table). If deletions only ever leave tombstones behind with
        nothing to reclaim them, enough churn saturates every bucket in the
        shard's hash table with tombstones (plus whatever's still occupied),
        and a subsequent lookup for a key that was never there raises
        `BackendError` instead of cleanly reporting "not found". Churning
        well past the raw table capacity must not trigger that.
        """
        backend = mp_backend
        capacity = backend._shard_hash_table_capacity

        for i in range(capacity * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        # Must return None cleanly, not raise BackendError from an
        # exhausted, all-tombstoned probe sequence.
        assert await backend.get(backend.get_key("never-existed")) is None

    async def test_live_keys_survive_a_rebuild(self, mp_backend) -> None:
        """
        A rebuild must preserve every live key's value - only tombstoned
        (deleted) entries should be dropped, never occupied ones.
        """
        backend = mp_backend
        threshold = backend._tombstone_rebuild_threshold_count

        survivor = backend.get_key("survivor")
        await backend.set(survivor, "unchanged")

        # Churn enough distinct keys to force at least one rebuild.
        for i in range(threshold * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        assert await backend.get(survivor) == "unchanged"

    async def test_deleted_keys_stay_deleted_after_rebuild(self, mp_backend) -> None:
        """A rebuild must not resurrect a key that was actually deleted."""
        backend = mp_backend
        threshold = backend._tombstone_rebuild_threshold_count

        gone = backend.get_key("gone")
        await backend.set(gone, "v")
        await backend.delete(gone)

        for i in range(threshold * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        assert await backend.get(gone) is None

    async def test_rebuild_does_not_disturb_differently_prefixed_keys(
        self, mp_backend
    ) -> None:
        """
        A rebuild reshuffles bucket positions for the whole shard. A key
        belonging to a different namespace prefix, sharing the same shard,
        must come through unchanged - `_rebuild_hash_table` operates on
        every occupied entry in the shard, not just ones matching whatever
        triggered it.
        """
        backend = mp_backend
        threshold = backend._tombstone_rebuild_threshold_count

        # Bypass `get_key()` to place a key under an unrelated namespace
        # prefix directly, without needing a second backend instance
        # attached to the same shared-memory segment.
        other_key = "other_ns:shared-shard-key"
        backend._set(other_key, "other-value", None)

        for i in range(threshold * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        assert backend._get(other_key) == "other-value"
