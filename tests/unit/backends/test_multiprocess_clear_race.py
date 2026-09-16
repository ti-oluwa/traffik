"""
Regression test for a slot-reuse race in `MultiProcessInMemoryBackend._clear()`.

`_clear()` removes matching keys from a shard's hash table and pushes their
slots back to the free pool under `slot_map_semaphore`, then separately
acquires `shard_semaphore` to clear the slots' occupied flags. In the gap
between those two acquisitions, a concurrent write for a different key can
pop one of the just-freed slots, insert its own hash-table entry, and write
fresh data into it, only for `_clear()`'s second phase to then blindly
overwrite that slot's occupied flag, silently destroying the concurrent
write even though the hash table still correctly points at it.
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
    pytest.mark.concurrent,
    pytest.mark.skipif(
        not SUPPORTS_FORK,
        reason="`MultiProcessInMemoryBackend` requires the 'fork' start method.",
    ),
]


@pytest.fixture
async def mp_backend():
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    backend = MultiProcessInMemoryBackend(
        namespace="clear_race_test",
        # A single shard with only two slots keeps things deterministic:
        # key1 takes the first slot off the (LIFO) free stack; once `_clear()`
        # frees it back, it's the only pending allocation, so key2's `_set()`
        # is guaranteed to pop that exact same slot back off the stack.
        number_of_shards=1,
        max_keys=2,
    )
    async with backend(close_on_exit=True):
        yield backend


class TestClearSlotReuseRace:
    async def test_clear_does_not_wipe_slot_reclaimed_mid_flight(
        self, mp_backend, monkeypatch: pytest.MonkeyPatch
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
