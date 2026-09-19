"""Concurrency-sensitive regression tests for `MultiProcessInMemoryBackend`"""

import asyncio
import multiprocessing
import platform
import threading
import typing

import pytest

if typing.TYPE_CHECKING:
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

SUPPORTS_FORK = (
    platform.system() != "Windows" and "fork" in multiprocessing.get_all_start_methods()
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.backend,
    pytest.mark.concurrent,
    pytest.mark.skipif(
        not SUPPORTS_FORK,
        reason="`MultiProcessInMemoryBackend` requires the 'fork' start method.",
    ),
]


@pytest.fixture
async def mp_backend():
    """
    Small backend for the `_clear()` race test. A single shard with only
    two slots keeps things deterministic. key1 takes the first slot off
    the (LIFO) free stack; once `_clear()` frees it back, it's the only
    pending allocation, so key2's `_set()` is guaranteed to pop that exact
    same slot back off the stack.
    """
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(
        namespace="clear_race_test",
        number_of_shards=1,
        max_keys=2,
    )
    async with backend(close_on_exit=True):
        yield backend


@pytest.fixture
async def mp_backend_tombstones():
    """
    Larger single-shard backend, with an explicit low rebuild threshold,
    for churning many keys through in the tombstone-reclamation tests.
    """
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(
        namespace="tombstone_test",
        number_of_shards=1,
        max_keys=16,
        tombstone_rebuild_threshold=0.2,
    )
    async with backend(close_on_exit=True):
        yield backend


@pytest.fixture
async def mp_backend_cleaner():
    """Single-shard backend for the background-cleaner race test."""
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(
        namespace="cleaner_race_test",
        number_of_shards=1,
        max_keys=8,
    )
    async with backend(close_on_exit=True):
        yield backend


class TestClearSlotReuseRace:
    """
    `_clear()` removes matching keys from a shard's hash table and pushes
    their slots back to the free pool under `slot_map_semaphore`, then
    separately acquires `shard_semaphore` to clear the slots' occupied
    flags. In the gap between those two acquisitions, a concurrent write
    for a different key can pop one of the just-freed slots, insert its
    own hash-table entry, and write fresh data into it, only for
    `_clear()`'s second phase to then blindly overwrite that slot's
    occupied flag, silently destroying the concurrent write even though
    the hash table still correctly points at it.
    """

    async def test_clear_does_not_wipe_slot_reclaimed_mid_flight(
        self, mp_backend: "MultiProcessInMemoryBackend", monkeypatch: pytest.MonkeyPatch
    ):
        backend = mp_backend
        key1 = backend.get_key("key1")
        key2 = backend.get_key("key2")

        await backend.set(key1, "v1")

        shard_idx = backend._get_shard_idx_for_key(key1)
        assert shard_idx == backend._get_shard_idx_for_key(key2)

        shard_semaphore = backend._shard_semaphores[shard_idx]  # type: ignore[index]
        real_acquire = shard_semaphore.acquire

        def racing_acquire(*args, **kwargs):
            # Fires once, right as `_clear()`'s second phase is about to
            # acquire `shard_semaphore`, simulating a concurrent `_set()`
            # for a different key that reclaims the slot `_clear()` just
            # freed, in the window between its two semaphore acquisitions.
            # Restore the real `acquire` first so `_set()`'s own acquire of
            # this same semaphore doesn't recurse back into this hook.
            monkeypatch.setattr(shard_semaphore, "acquire", real_acquire)
            backend._set(key2, "v2", None)
            return real_acquire(*args, **kwargs)

        monkeypatch.setattr(shard_semaphore, "acquire", racing_acquire)

        await backend.clear()

        # key2's concurrently-written value must survive `_clear()` ...
        assert await backend.get(key2) == "v2"
        # ... while key1, which `_clear()` actually targeted, is gone.
        assert await backend.get(key1) is None


class TestTombstoneRebuildThreshold:
    """`tombstone_rebuild_threshold` validation at construction time."""

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
    """
    Linear probing means a deleted key leaves a tombstone rather than a
    genuinely empty bucket. Left unchecked, tombstones accumulate under
    key churn (TTL expiry, `delete`, repeated `clear`) and probe chains
    grow over time, degrading lookups toward O(shard capacity) even while
    the live key count stays flat. The backend tracks a per-shard
    tombstone counter and rebuilds that shard's hash table in place once
    the counter crosses `tombstone_rebuild_threshold * shard_hash_table_capacity`.
    """

    async def test_tombstone_count_resets_once_threshold_is_crossed(
        self, mp_backend_tombstones: "MultiProcessInMemoryBackend"
    ) -> None:
        """
        Deleting keys past the configured threshold must trigger a rebuild,
        evidenced by the tombstone counter dropping back down instead of
        growing without bound.
        """
        backend = mp_backend_tombstones
        shard_base = 0
        threshold = backend._tombstone_rebuild_threshold_count

        counts = []
        assert backend._buffer is not None
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
        self, mp_backend_tombstones: "MultiProcessInMemoryBackend"
    ) -> None:
        """
        This is the concrete failure mode tombstone reclamation exists to
        prevent. With linear probing, a lookup for an absent key only
        terminates when it hits a genuinely empty bucket (or scans the
        whole table). If deletions only ever leave tombstones behind with
        nothing to reclaim them, enough churn saturates every bucket in the
        shard's hash table with tombstones (plus whatever's still occupied),
        and a subsequent lookup for a key that was never there raises
        `BackendError` instead of cleanly reporting "not found". Churning
        well past the raw table capacity must not trigger that.
        """
        backend = mp_backend_tombstones
        capacity = backend._shard_hash_table_capacity

        for i in range(capacity * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        # Must return None cleanly, not raise BackendError from an
        # exhausted, all-tombstoned probe sequence.
        assert await backend.get(backend.get_key("never-existed")) is None

    async def test_live_keys_survive_a_rebuild(
        self, mp_backend_tombstones: "MultiProcessInMemoryBackend"
    ) -> None:
        """
        A rebuild must preserve every live key's value - only tombstoned
        (deleted) entries should be dropped, never occupied ones.
        """
        backend = mp_backend_tombstones
        threshold = backend._tombstone_rebuild_threshold_count

        survivor = backend.get_key("survivor")
        await backend.set(survivor, "unchanged")

        # Churn enough distinct keys to force at least one rebuild.
        for i in range(threshold * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        assert await backend.get(survivor) == "unchanged"

    async def test_deleted_keys_stay_deleted_after_rebuild(
        self, mp_backend_tombstones: "MultiProcessInMemoryBackend"
    ) -> None:
        """A rebuild must not resurrect a key that was actually deleted."""
        backend = mp_backend_tombstones
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
        self, mp_backend_tombstones: "MultiProcessInMemoryBackend"
    ) -> None:
        """
        A rebuild reshuffles bucket positions for the whole shard. A key
        belonging to a different namespace prefix, sharing the same shard,
        must come through unchanged - `_rebuild_hash_table` operates on
        every occupied entry in the shard, not just ones matching whatever
        triggered it.
        """
        backend = mp_backend_tombstones
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


class TestCleanerSlotReuseRace:
    """
    `_sample_and_reap_shard` used to mutate a slot's occupied flag under
    only `slot_map_semaphore`, without ever acquiring `shard_semaphore`.
    That let a concurrent `_set`/`_increment` call, one that had already
    passed its own `shard_semaphore`-guarded generation check for a slot
    and was in the middle of writing to it, have its result silently
    clobbered by the cleaner's unsynchronized `_clear_slot` call, because
    the two mutations were never mutually exclusive. Unlike the `_clear()`
    race above, `_free_stack_push`'s generation bump does not help here:
    that mechanism guards a *future* generation check against a concurrent
    free, not a write already in flight under a generation that was, and
    still is, valid.
    """

    async def test_cleaner_does_not_wipe_a_write_that_lands_mid_check(
        self,
        mp_backend_cleaner: "MultiProcessInMemoryBackend",
        monkeypatch: pytest.MonkeyPatch,
    ):
        backend = mp_backend_cleaner
        key1 = backend.get_key("key1")

        # Seed key1 and let it genuinely expire.
        await backend.set(key1, "old", 0.01)
        await asyncio.sleep(0.05)

        shard_idx = backend._get_shard_idx_for_key(key1)
        capacity = backend._shard_hash_table_capacity

        # A writer thread that gets paused right after it passes its
        # `shard_semaphore`-guarded generation check, but before it
        # actually writes - simulating "a legitimate write is in flight".
        writer_paused = threading.Event()
        release_writer = threading.Event()
        real_write = backend._write_string_slot

        def hooked_write(*args, **kwargs):
            writer_paused.set()
            assert release_writer.wait(timeout=5), "test orchestration timed out"
            return real_write(*args, **kwargs)

        monkeypatch.setattr(backend, "_write_string_slot", hooked_write)

        writer_thread = threading.Thread(
            target=backend._set, args=(key1, "refreshed", 100.0)
        )
        writer_thread.start()
        assert writer_paused.wait(timeout=5), "writer never reached its write hook"

        # Start the cleaner while the writer holds `shard_semaphore`,
        # mid-write. Fixed, the cleaner blocks trying to acquire the same
        # semaphore and can't finish yet; buggy, it needs no lock the
        # writer holds and finishes almost immediately.
        cleaner_thread = threading.Thread(
            target=backend._sample_and_reap_shard, args=(shard_idx, capacity)
        )
        cleaner_thread.start()
        cleaner_thread.join(timeout=0.5)

        release_writer.set()
        writer_thread.join(timeout=5)
        cleaner_thread.join(timeout=5)

        # The writer's refresh - which lands a future expiry - must survive
        # regardless of how the cleaner's and writer's steps interleaved.
        assert await backend.get(key1) == "refreshed"
