"""Concurrency-sensitive regression tests for `MultiProcessInMemoryBackend`"""

import asyncio
import multiprocessing
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import typing
from multiprocessing.synchronize import Event
from unittest.mock import patch

import pytest

from traffik.backends.multiprocess import (
    _HASH_TABLE_ENTRY_SIZE,
    _HASH_TABLE_LOAD_FACTOR,
    _HASH_TABLE_STATE_OFFSET,
    _HASH_TABLE_WRITING_STATE,
    _SLOT_WRITING,
    _UINT8_STRUCT,
    MultiProcessInMemoryBackend,
)
from traffik.exceptions import BackendError, ShardUnavailableError

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
async def backend():
    """
    Small backend for the `_clear()` race test. A single shard with only
    two slots keeps things deterministic. key1 takes the first slot off
    the (LIFO) free stack; once `_clear()` frees it back, it's the only
    pending allocation, so key2's `_set()` is guaranteed to pop that exact
    same slot back off the stack.
    """
    backend = MultiProcessInMemoryBackend(
        namespace="clear_race_test",
        number_of_shards=1,
        max_keys=2,
    )
    async with backend(close_on_exit=True):
        yield backend


@pytest.fixture
async def tombstones_backend():
    """
    Larger single-shard backend, with an explicit low rebuild threshold,
    for churning many keys through in the tombstone-reclamation tests.
    """
    backend = MultiProcessInMemoryBackend(
        namespace="tombstone_test",
        number_of_shards=1,
        max_keys=16,
        tombstone_rebuild_threshold=0.2,
    )
    async with backend(close_on_exit=True):
        yield backend


@pytest.fixture
async def cleaner_backend():
    """Single-shard backend for the background-cleaner race test."""
    backend = MultiProcessInMemoryBackend(
        namespace="cleaner_race_test",
        number_of_shards=1,
        max_keys=8,
    )
    async with backend(close_on_exit=True):
        yield backend


@pytest.fixture
async def started_backend():
    """
    Single-shard backend whose segment is allocated (`start()`) but not yet
    entered, so a child forked from it inherits a live mapping. The short
    `semaphore_timeout` keeps stale-owner recovery fast.
    """
    backend = MultiProcessInMemoryBackend(
        namespace="started_test",
        number_of_shards=1,
        max_keys=4,
        semaphore_timeout=1.0,
    )
    backend.start()
    try:
        yield backend
    finally:
        await backend.close()


@pytest.fixture
async def full_shard_backend():
    """
    Single-shard backend with no background cleanup, so nothing but the
    operations under test can ever reclaim a slot.
    """
    backend = MultiProcessInMemoryBackend(
        namespace="full_shard_test",
        number_of_shards=1,
        max_keys=8,
        cleanup_frequency=None,
    )
    async with backend(close_on_exit=True):
        yield backend


def run_child(target, *args) -> multiprocessing.Process:
    """Fork `target(*args)` as a real child process and wait for it to exit."""
    process = multiprocessing.Process(target=target, args=args)
    process.start()
    process.join(timeout=5)
    return process


def acquire_shard_then_sigkill(backend: MultiProcessInMemoryBackend) -> None:
    """
    Acquire shard 0's semaphore through the real guarded path (so the owner
    record is populated exactly as a real write would), then die uncatchably
    while holding it.
    """
    backend._acquire_shard(0)
    os.kill(os.getpid(), signal.SIGKILL)


def acquire_slot_map_then_sigkill(backend: MultiProcessInMemoryBackend) -> None:
    """Same as above, for the slot-map (hash-table) semaphore."""
    backend._acquire_slot_map(0)
    os.kill(os.getpid(), signal.SIGKILL)


def leave_slot_writing_then_sigkill(
    backend: MultiProcessInMemoryBackend, slot_idx: int
) -> None:
    """
    Die midway through `_write_string_slot`: after the slot is marked
    `_SLOT_WRITING`, before the write completes and it's marked occupied.
    """
    backend._acquire_shard(0)
    shard_base = backend._get_shard_base(0)
    offset = backend._get_slot_offset(shard_base, slot_idx)
    assert backend._buffer is not None
    backend._OCCUPIED_FLAG_STRUCT.pack_into(
        backend._buffer, offset + backend._occupied_flag_offset, _SLOT_WRITING
    )
    os.kill(os.getpid(), signal.SIGKILL)


def leave_hash_entry_writing_then_sigkill(
    backend: MultiProcessInMemoryBackend, key: str
) -> None:
    """
    Die midway through `_hash_table_upsert`: after the bucket for `key` is
    marked `_HASH_TABLE_WRITING_STATE`, before the insert completes.
    """
    backend._acquire_slot_map(0)
    shard_base = backend._get_shard_base(0)
    assert backend._buffer is not None
    idx = backend._hash_table_lookup(backend._buffer, shard_base, key.encode("utf-8"))
    entry_offset = (
        shard_base
        + backend._shard_hash_table_base_offset
        + idx * _HASH_TABLE_ENTRY_SIZE
    )
    _UINT8_STRUCT.pack_into(
        backend._buffer,
        entry_offset + _HASH_TABLE_STATE_OFFSET,
        _HASH_TABLE_WRITING_STATE,
    )
    os.kill(os.getpid(), signal.SIGKILL)


def hold_shard_alive(
    backend: MultiProcessInMemoryBackend, hold_seconds: float, ready: Event
) -> None:
    """A genuine, slow-but-alive holder must never be treated as stale."""
    backend._acquire_shard(0)
    ready.set()
    time.sleep(hold_seconds)
    backend._release_shard(0)


def read_then_write_across_fork(
    backend: MultiProcessInMemoryBackend, results: dict[str, typing.Any]
) -> None:
    """
    Runs in the forked child, on its own event loop since the parent's
    doesn't survive a fork, and exercises the executor `fork` had to rebuild.
    """

    async def run() -> None:
        # `persistent=True`: entering must not reset the data being shared.
        async with backend(persistent=True, close_on_exit=False):
            results["saw_parent_value"] = await backend.get(
                backend.get_key("from_parent")
            )
            await backend.set(backend.get_key("from_child"), "child-value")
            results["child_pid"] = backend._pid

    asyncio.run(run())


async def fill_with_expiring_keys(backend: MultiProcessInMemoryBackend) -> None:
    """Fill every slot with a key, then let them all expire unreclaimed."""
    for i in range(backend._max_keys_per_shard):
        await backend.set(backend.get_key(f"old{i}"), "x", expire=0.2)
    await asyncio.sleep(0.3)


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
        self, backend: MultiProcessInMemoryBackend, monkeypatch: pytest.MonkeyPatch
    ):
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
        headroom = 1 - _HASH_TABLE_LOAD_FACTOR
        for bad in (headroom, headroom + 0.1, 0.0, -0.1, 1.0):
            with pytest.raises(ValueError):
                MultiProcessInMemoryBackend(tombstone_rebuild_threshold=bad)

    def test_accepts_threshold_within_headroom(self):
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
        self, tombstones_backend: MultiProcessInMemoryBackend
    ) -> None:
        """
        Deleting keys past the configured threshold must trigger a rebuild,
        evidenced by the tombstone counter dropping back down instead of
        growing without bound.
        """
        backend = tombstones_backend
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
        self, tombstones_backend: MultiProcessInMemoryBackend
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
        backend = tombstones_backend
        capacity = backend._shard_hash_table_capacity

        for i in range(capacity * 3):
            key = backend.get_key(f"churn-{i}")
            await backend.set(key, "v")
            await backend.delete(key)

        # Must return None cleanly, not raise BackendError from an
        # exhausted, all-tombstoned probe sequence.
        assert await backend.get(backend.get_key("never-existed")) is None

    async def test_live_keys_survive_a_rebuild(
        self, tombstones_backend: MultiProcessInMemoryBackend
    ) -> None:
        """
        A rebuild must preserve every live key's value - only tombstoned
        (deleted) entries should be dropped, never occupied ones.
        """
        backend = tombstones_backend
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
        self, tombstones_backend: MultiProcessInMemoryBackend
    ) -> None:
        """A rebuild must not resurrect a key that was actually deleted."""
        backend = tombstones_backend
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
        self, tombstones_backend: MultiProcessInMemoryBackend
    ) -> None:
        """
        A rebuild reshuffles bucket positions for the whole shard. A key
        belonging to a different namespace prefix, sharing the same shard,
        must come through unchanged - `_rebuild_hash_table` operates on
        every occupied entry in the shard, not just ones matching whatever
        triggered it.
        """
        backend = tombstones_backend
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
        cleaner_backend: MultiProcessInMemoryBackend,
        monkeypatch: pytest.MonkeyPatch,
    ):
        backend = cleaner_backend
        key1 = backend.get_key("key1")

        # Seed key1 and let it genuinely expire.
        await backend.set(key1, "old", 0.01)
        await asyncio.sleep(0.05)

        shard_idx = backend._get_shard_idx_for_key(key1)
        capacity = backend._shard_hash_table_capacity

        # A writer thread that gets paused right after it passes its
        # `shard_semaphore`-guarded generation check, but before it
        # actually writes; simulating "a legitimate write is in flight".
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


class TestEstimateSharedMemorySize:
    """
    `estimate_shared_memory_size` re-implements the same formula
    `__init__` uses to actually size the shared-memory segment.

    These tests exist to catch the two formulas drifting apart if either is
    changed without the other.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"max_keys": 16384, "number_of_shards": 32, "max_value_size": 1024},
            {"max_keys": 65536, "number_of_shards": 64, "max_value_size": 2048},
            {"max_keys": 1000, "number_of_shards": 4, "max_value_size": 100},
            {
                "max_keys": 1_000_000,
                "number_of_shards": 128,
                "max_value_size": 64,
                "lock_pool_size": 256,
                "lock_pool_headroom": 8,
            },
        ],
    )
    def test_matches_actual_instance_size(self, kwargs):
        estimated = MultiProcessInMemoryBackend.estimate_shared_memory_size(**kwargs)
        backend = MultiProcessInMemoryBackend(**kwargs)
        assert backend.shared_memory_size == estimated


class TestPlatformGuards:
    """
    The module imports on every platform; only constructing the backend is
    restricted, to POSIX with the `fork`/`forkserver` start methods.
    """

    def test_rejects_windows(self):
        with patch("traffik.backends.multiprocess.ON_WINDOWS", True):
            with pytest.raises(RuntimeError, match="not supported on Windows"):
                MultiProcessInMemoryBackend(namespace="guard_windows")

    def test_rejects_spawn_start_method(self):
        with (
            patch("traffik.backends.multiprocess.ON_WINDOWS", False),
            patch(
                "traffik.backends.multiprocess.multiprocessing.get_start_method",
                return_value="spawn",
            ),
        ):
            with pytest.raises(RuntimeError, match="'fork' or 'forkserver'"):
                MultiProcessInMemoryBackend(namespace="guard_spawn")

    @pytest.mark.parametrize("method", ["fork", "forkserver"])
    def test_accepts_fork_and_forkserver(self, method: str):
        with patch(
            "traffik.backends.multiprocess.multiprocessing.get_start_method",
            return_value=method,
        ):
            MultiProcessInMemoryBackend(namespace=f"guard_{method}")

    def test_module_imports_without_posix_only_modules(self):
        """
        Importing the module must not need `fcntl` (absent on Windows): it's
        pulled in by `traffik.backends`, so a hard import would break that
        whole package there, not just this backend. Simulated in a fresh
        interpreter, since the module here is already imported.
        """
        code = (
            "import platform, sys\n"
            "sys.modules['fcntl'] = None\n"
            "platform.system = lambda: 'Windows'\n"
            "import traffik.backends.multiprocess as module\n"
            "assert module.ON_WINDOWS\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


class TestSemaphoreTimeoutValidation:
    """`semaphore_timeout` validation at construction time."""

    def test_rejects_non_positive_timeout(self):
        with pytest.raises(ValueError, match="semaphore_timeout"):
            MultiProcessInMemoryBackend(semaphore_timeout=0)

    def test_accepts_positive_timeout(self):
        MultiProcessInMemoryBackend(semaphore_timeout=0.5)


class TestStaleShardOwnerRecovery:
    """
    `_shard_semaphores`/`_slot_map_semaphores` are plain
    `multiprocessing.Semaphore`s; correct under normal operation, but a POSIX
    semaphore is never released by the OS when its holder dies (`SIGKILL`,
    OOM-kill, a segfault, anything that skips Python's own cleanup entirely).
    Before the fix under test, a worker dying mid-write left its semaphore
    permanently held, and every other process touching the same shard blocked
    forever with no way to recover short of restarting the whole segment.
    These tests exercise the fix with a killed `multiprocessing.Process`
    holding the semaphore.
    """

    async def test_recovers_from_dead_shard_owner(self, started_backend):
        process = run_child(acquire_shard_then_sigkill, started_backend)
        assert process.exitcode == -signal.SIGKILL

        async with started_backend(persistent=False, close_on_exit=True):
            key = started_backend.get_key("k")
            await asyncio.wait_for(started_backend.set(key, "v"), timeout=5)
            assert await started_backend.get(key) == "v"

    async def test_recovers_from_dead_slot_map_owner(self, started_backend):
        process = run_child(acquire_slot_map_then_sigkill, started_backend)
        assert process.exitcode == -signal.SIGKILL

        async with started_backend(persistent=False, close_on_exit=True):
            key = started_backend.get_key("k")
            await asyncio.wait_for(started_backend.set(key, "v"), timeout=5)
            assert await started_backend.get(key) == "v"

    async def test_genuinely_alive_holder_is_not_falsely_recovered(
        self, started_backend
    ):
        """
        The critical safety property: a slow-but-alive holder must never be
        treated as stale, or a live process could have its semaphore pulled
        out from under it.
        """
        ready = multiprocessing.Event()
        process = multiprocessing.Process(
            target=hold_shard_alive, args=(started_backend, 2.5, ready)
        )
        process.start()
        assert ready.wait(timeout=5), "child never signaled it had acquired the shard"

        async with started_backend(persistent=False, close_on_exit=True):
            with pytest.raises(ShardUnavailableError):
                await started_backend.set(started_backend.get_key("k"), "v")

            # Wait for the genuine holder to finish and release before this
            # block exits: exit triggers a reset() touching the same shard,
            # which would itself raise if the holder (correctly) still had it.
            process.join(timeout=5)
            assert process.exitcode == 0, "the genuine holder must exit untouched"

    def test_shard_unavailable_error_is_a_backend_error(self):
        assert issubclass(ShardUnavailableError, BackendError)


class TestTornWriteRecovery:
    """
    A crash left mid-write must never expose a mix of old and new fields to a
    later reader. See `_SLOT_WRITING`/`_HASH_TABLE_WRITING_STATE`.
    """

    async def test_torn_slot_write_is_reclaimed_not_exposed(self, started_backend):
        backend = started_backend
        async with backend(persistent=False, close_on_exit=True):
            # A real, hash-table-linked slot for "k" first (as `_set` would
            # make), so recovery's job is specifically to notice the WRITING
            # flag left on an otherwise-valid slot.
            key = backend.get_key("k")
            await backend.set(key, "original")
            assert backend._buffer is not None
            slot_idx = backend._hash_table_get_slot(
                backend._buffer, backend._get_shard_base(0), key.encode("utf-8")
            )
            assert slot_idx is not None

            # Forked while the buffer is still mapped: forking after this
            # block exits would hand the child a closed backend.
            process = run_child(leave_slot_writing_then_sigkill, backend, slot_idx)
            assert process.exitcode == -signal.SIGKILL

            # Reads as absent, never as a mix of old value and partial state...
            assert await backend.get(key) is None
            # ...and is normally writable again afterwards.
            await asyncio.wait_for(backend.set(key, "fresh"), timeout=5)
            assert await backend.get(key) == "fresh"

    async def test_torn_hash_table_write_is_reclaimed(self, started_backend):
        process = run_child(leave_hash_entry_writing_then_sigkill, started_backend, "k")
        assert process.exitcode == -signal.SIGKILL

        async with started_backend(persistent=False, close_on_exit=True):
            key = started_backend.get_key("k")
            assert await started_backend.get(key) is None
            await asyncio.wait_for(started_backend.set(key, "fresh"), timeout=5)
            assert await started_backend.get(key) == "fresh"


class TestFullOfExpiredKeys:
    """
    A rate limiter's keys expire by design and its key space churns (client
    IPs, user ids). Before the fix under test, a shard whose slots were all
    held by *expired* keys rejected every new key with "`max_keys_per_shard`
    reached": reads already treated those keys as absent, but nothing gave
    their slots back unless a background cleanup happened to run, and
    `cleanup_frequency` defaults to `None`.
    """

    async def test_set_reclaims_expired_slots(self, full_shard_backend):
        await fill_with_expiring_keys(full_shard_backend)
        key = full_shard_backend.get_key("new")
        await full_shard_backend.set(key, "fresh", expire=30)
        assert await full_shard_backend.get(key) == "fresh"

    async def test_increment_reclaims_expired_slots(self, full_shard_backend):
        await fill_with_expiring_keys(full_shard_backend)
        # Exactly 1, not 2: the failed first attempt must not have applied
        # the increment before the retry did.
        key = full_shard_backend.get_key("ctr")
        assert await full_shard_backend.increment(key, amount=1) == 1

    async def test_increment_with_ttl_reclaims_expired_slots(self, full_shard_backend):
        await fill_with_expiring_keys(full_shard_backend)
        key = full_shard_backend.get_key("ctr")
        assert await full_shard_backend.increment_with_ttl(key, amount=1, ttl=30) == 1

    async def test_multi_set_reclaims_expired_slots(self, full_shard_backend):
        await fill_with_expiring_keys(full_shard_backend)
        items = {full_shard_backend.get_key(f"new{i}"): f"v{i}" for i in range(4)}
        await full_shard_backend.multi_set(items, expire=30)
        for key, value in items.items():
            assert await full_shard_backend.get(key) == value

    async def test_reclaims_repeatedly_not_just_once(self, full_shard_backend):
        for round_ in range(3):
            await fill_with_expiring_keys(full_shard_backend)
            key = full_shard_backend.get_key(f"r{round_}")
            await full_shard_backend.set(key, "v", expire=30)
            assert await full_shard_backend.get(key) == "v"
            await full_shard_backend.delete(key)

    async def test_mixed_live_and_expired_reclaims_only_the_expired(
        self, full_shard_backend
    ):
        backend = full_shard_backend
        half = backend._max_keys_per_shard // 2
        for i in range(half):
            await backend.set(backend.get_key(f"live{i}"), f"v{i}", expire=60)
        for i in range(half):
            await backend.set(backend.get_key(f"dead{i}"), "x", expire=0.2)
        await asyncio.sleep(0.3)

        for i in range(half):
            await backend.set(backend.get_key(f"new{i}"), "n", expire=60)

        for i in range(half):
            assert await backend.get(backend.get_key(f"live{i}")) == f"v{i}"
            assert await backend.get(backend.get_key(f"new{i}")) == "n"


class TestGenuinelyFullShard:
    """A shard genuinely full of *live* keys must still fail loudly."""

    async def test_full_of_live_keys_still_fails_loudly(self, full_shard_backend):
        backend = full_shard_backend
        capacity = backend._max_keys_per_shard
        for i in range(capacity):
            await backend.set(backend.get_key(f"live{i}"), f"v{i}", expire=60)

        with pytest.raises(BackendError, match="max_keys_per_shard"):
            await backend.set(backend.get_key("overflow"), "z")

        # Trying to reclaim must not have evicted anything live.
        for i in range(capacity):
            assert await backend.get(backend.get_key(f"live{i}")) == f"v{i}"


class TestForkInheritance:
    """
    The shared-memory mapping and semaphores are inherited correctly by
    `fork()`, but thread- and event-loop-bound state (the executor, the cleanup
    task, the cached pid) is not, so `_reinitialize_after_fork` rebuilds those
    in the child. These tests use a real forked child, not a simulated one.
    """

    async def test_data_is_shared_in_both_directions(self, started_backend):
        backend = started_backend
        async with backend(persistent=True, close_on_exit=False):
            await backend.set(backend.get_key("from_parent"), "parent-value")

            with multiprocessing.Manager() as manager:
                results = manager.dict()
                process = run_child(read_then_write_across_fork, backend, results)
                assert process.exitcode == 0
                results = dict(results)

            # Parent -> child: written before the fork, visible after it.
            assert results["saw_parent_value"] == "parent-value"
            # Child -> parent: written after the fork, visible here.
            assert await backend.get(backend.get_key("from_child")) == "child-value"
            # The child refreshed its cached pid, which owner records rely on.
            assert results["child_pid"] != backend._pid
