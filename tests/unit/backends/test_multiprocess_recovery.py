"""
Regression tests for `MultiProcessInMemoryBackend`'s stale-owner recovery.

`_shard_semaphores`/`_slot_map_semaphores` are plain `multiprocessing.Semaphore`s;
correct under normal operation, but a POSIX semaphore is never released by the
OS when its holder dies (`SIGKILL`, OOM-kill, a segfault, anything that skips
Python's own cleanup entirely). Before the fix under test here, a worker dying
mid-write left that permanently held, and every other process touching the same
shard blocked forever with no way to recover short of restarting the whole
segment. These tests exercise the fix with a killed `multiprocessing.Process`
holding the semaphore.
"""

import asyncio
import multiprocessing
import os
import platform
import signal
import time
import typing
from multiprocessing.synchronize import Event

import pytest

from traffik.exceptions import BackendError, ShardUnavailableError

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


def acquire_shard_then_sigkill(backend: "MultiProcessInMemoryBackend") -> None:
    """
    Acquire shard 0's semaphore through the real guarded
    path (so the owner record is correctly populated, exactly like a real
    write operation would), then die uncatchably while holding it.
    """
    backend._acquire_shard(0)
    os.kill(os.getpid(), signal.SIGKILL)


def acquire_slot_map_then_sigkill(backend: "MultiProcessInMemoryBackend") -> None:
    """Same as above, for the slot-map (hash-table) semaphore."""
    backend._acquire_slot_map(0)
    os.kill(os.getpid(), signal.SIGKILL)


def leave_slot_writing_then_sigkill(
    backend: "MultiProcessInMemoryBackend", slot_idx: int
) -> None:
    """
    Simulate dying midway through `_write_string_slot`,
    after the slot is marked `_SLOT_WRITING` but before the write
    completes and it's marked `_SLOT_OCCUPIED`.
    """
    from traffik.backends.multiprocess import _SLOT_WRITING

    backend._acquire_shard(0)
    shard_base = backend._get_shard_base(0)
    offset = backend._get_slot_offset(shard_base, slot_idx)
    assert backend._buffer is not None
    backend._OCCUPIED_FLAG_STRUCT.pack_into(
        backend._buffer, offset + backend._occupied_flag_offset, _SLOT_WRITING
    )
    os.kill(os.getpid(), signal.SIGKILL)


def leave_hash_entry_writing_then_sigkill(
    backend: "MultiProcessInMemoryBackend", key: str
) -> None:
    """
    Simulate dying midway through `_hash_table_upsert`, after the bucket
    for `key` is marked `_HASH_TABLE_WRITING_STATE` but before the insert completes.
    """
    from traffik.backends.multiprocess import (
        _HASH_TABLE_ENTRY_SIZE,
        _HASH_TABLE_STATE_OFFSET,
        _HASH_TABLE_WRITING_STATE,
        _UINT8_STRUCT,
    )

    backend._acquire_slot_map(0)
    shard_base = backend._get_shard_base(0)
    key_bytes = key.encode("utf-8")
    assert backend._buffer is not None
    idx = backend._hash_table_lookup(backend._buffer, shard_base, key_bytes)
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
    backend: "MultiProcessInMemoryBackend", hold_seconds: float, ready: Event
) -> None:
    """A genuine, slow-but-alive holder must never be treated as stale."""
    backend._acquire_shard(0)
    ready.set()
    time.sleep(hold_seconds)
    backend._release_shard(0)


class TestStaleShardOwnerRecovery:
    async def test_recovers_from_dead_shard_owner(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="recovery_shard", number_of_shards=1, semaphore_timeout=1.0
        )
        backend.start()
        try:
            process = multiprocessing.Process(
                target=acquire_shard_then_sigkill, args=(backend,)
            )
            process.start()
            process.join(timeout=5)
            assert process.exitcode == -signal.SIGKILL

            async with backend(persistent=False, close_on_exit=True):
                key = backend.get_key("k")
                await asyncio.wait_for(backend.set(key, "v"), timeout=5)
                assert await backend.get(key) == "v"
        finally:
            await backend.close()

    async def test_recovers_from_dead_slot_map_owner(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="recovery_slot_map", number_of_shards=1, semaphore_timeout=1.0
        )
        backend.start()
        try:
            process = multiprocessing.Process(
                target=acquire_slot_map_then_sigkill, args=(backend,)
            )
            process.start()
            process.join(timeout=5)
            assert process.exitcode == -signal.SIGKILL

            async with backend(persistent=False, close_on_exit=True):
                key = backend.get_key("k")
                await asyncio.wait_for(backend.set(key, "v"), timeout=5)
                assert await backend.get(key) == "v"
        finally:
            await backend.close()

    async def test_genuinely_alive_holder_is_not_falsely_recovered(self) -> None:
        """The critical safety property: a slow-but-alive holder must never
        be treated as stale, or a live process could have its semaphore
        pulled out from under it.
        """
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="recovery_no_false_positive",
            number_of_shards=1,
            semaphore_timeout=1.0,
        )
        backend.start()
        try:
            ready = multiprocessing.Event()
            process = multiprocessing.Process(
                target=hold_shard_alive, args=(backend, 2.5, ready)
            )
            process.start()
            assert ready.wait(timeout=5), (
                "child never signaled it had acquired the shard"
            )

            async with backend(persistent=False, close_on_exit=True):
                with pytest.raises(ShardUnavailableError):
                    await backend.set(backend.get_key("k"), "v")

                # Wait for the genuine holder to finish and release before
                # this block exits: exit triggers a reset() that touches
                # the same shard, which would itself raise if the holder
                # (correctly) still had it.
                process.join(timeout=5)
                assert process.exitcode == 0, (
                    "the genuine holder must exit normally, untouched"
                )
        finally:
            await backend.close()

    async def test_shard_unavailable_error_is_a_backend_error(self) -> None:
        assert issubclass(ShardUnavailableError, BackendError)


class TestTornWriteRecovery:
    """
    A crash left mid-write must never expose a mix of old and new
    fields to a later reader. See `_SLOT_WRITING`/`_HASH_TABLE_WRITING_STATE`.
    """

    async def test_torn_slot_write_is_reclaimed_not_exposed(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="recovery_torn_slot",
            number_of_shards=1,
            max_keys=4,
            semaphore_timeout=1.0,
        )
        backend.start()
        try:
            async with backend(persistent=False, close_on_exit=True):
                # Get a real, hash-table-linked slot for "k" first (as
                # `_set` normally would), so recovery's job is specifically
                # to notice the WRITING flag left on an otherwise-valid slot.
                await backend.set(backend.get_key("k"), "original")
                shard_base = backend._get_shard_base(0)
                assert backend._buffer is not None
                slot_idx = backend._hash_table_get_slot(
                    backend._buffer, shard_base, backend.get_key("k").encode("utf-8")
                )
                assert slot_idx is not None

                # Forked while the buffer is still mapped -- forking after
                # this block exits would hand the child a closed backend
                # (`close_on_exit=True` releases `self._buffer` on exit).
                process = multiprocessing.Process(
                    target=leave_slot_writing_then_sigkill, args=(backend, slot_idx)
                )
                process.start()
                process.join(timeout=5)
                assert process.exitcode == -signal.SIGKILL

                # The torn slot must read as absent, never as a mix of the
                # old value and whatever partial state the writer left.
                assert await backend.get(backend.get_key("k")) is None
                # And must be normally writable again afterwards.
                await asyncio.wait_for(
                    backend.set(backend.get_key("k"), "fresh"), timeout=5
                )
                assert await backend.get(backend.get_key("k")) == "fresh"
        finally:
            await backend.close()

    async def test_torn_hash_table_write_is_reclaimed(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        backend = MultiProcessInMemoryBackend(
            namespace="recovery_torn_hash",
            number_of_shards=1,
            max_keys=4,
            semaphore_timeout=1.0,
        )
        backend.start()
        try:
            process = multiprocessing.Process(
                target=leave_hash_entry_writing_then_sigkill, args=(backend, "k")
            )
            process.start()
            process.join(timeout=5)
            assert process.exitcode == -signal.SIGKILL

            async with backend(persistent=False, close_on_exit=True):
                key = backend.get_key("k")
                assert await backend.get(key) is None
                await asyncio.wait_for(backend.set(key, "fresh"), timeout=5)
                assert await backend.get(key) == "fresh"
        finally:
            await backend.close()


class TestSemaphoreTimeoutValidation:
    def test_rejects_non_positive_timeout(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        with pytest.raises(ValueError, match="semaphore_timeout"):
            MultiProcessInMemoryBackend(
                namespace="semaphore_timeout_validation", semaphore_timeout=0
            )

    def test_accepts_positive_timeout(self) -> None:
        from traffik.backends.multiprocess import MultiProcessInMemoryBackend

        MultiProcessInMemoryBackend(
            namespace="semaphore_timeout_validation_ok", semaphore_timeout=0.5
        )
