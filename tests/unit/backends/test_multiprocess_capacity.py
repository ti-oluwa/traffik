"""
Tests for `MultiProcessInMemoryBackend`'s behavior when a shard runs out of slots.

A rate limiter's keys expire by design and its key space churns (client IPs,
user ids). Before the fix under test, a shard whose slots were all held by
*expired* keys rejected every new key with "`max_keys_per_shard` reached":
reads already treated those keys as absent, but nothing gave their slots back
unless a background cleanup happened to run, and `cleanup_frequency` defaults
to `None`. A shard that is genuinely full of *live* keys must still fail loudly.
"""

import asyncio

import pytest

from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.exceptions import BackendError

pytestmark = [pytest.mark.anyio, pytest.mark.backend]

MAX_KEYS = 8


def get_backend(namespace: str) -> MultiProcessInMemoryBackend:
    return MultiProcessInMemoryBackend(
        namespace=namespace,
        number_of_shards=1,
        max_keys=MAX_KEYS,
        cleanup_frequency=None,  # nothing reclaims in the background
    )


async def fill_backend_with_expiring_keys(backend: MultiProcessInMemoryBackend) -> None:
    for i in range(MAX_KEYS):
        await backend.set(backend.get_key(f"old{i}"), "x", expire=1)
    await asyncio.sleep(1.2)  # every slot is now logically dead, none reclaimed


class TestFullOfExpiredKeys:
    async def test_set_reclaims_expired_slots(self) -> None:
        backend = get_backend("capacity_set")
        async with backend(persistent=False, close_on_exit=True):
            await fill_backend_with_expiring_keys(backend)
            await backend.set(backend.get_key("new"), "fresh", expire=30)
            assert await backend.get(backend.get_key("new")) == "fresh"

    async def test_increment_reclaims_expired_slots(self) -> None:
        backend = get_backend("capacity_increment")
        async with backend(persistent=False, close_on_exit=True):
            await fill_backend_with_expiring_keys(backend)
            # Exactly 1, not 2: the failed first attempt must not have
            # applied the increment before the retry did.
            assert await backend.increment(backend.get_key("ctr"), amount=1) == 1

    async def test_increment_with_ttl_reclaims_expired_slots(self) -> None:
        backend = get_backend("capacity_increment_ttl")
        async with backend(persistent=False, close_on_exit=True):
            await fill_backend_with_expiring_keys(backend)
            assert (
                await backend.increment_with_ttl(
                    backend.get_key("ctr"), amount=1, ttl=30
                )
                == 1
            )

    async def test_multi_set_reclaims_expired_slots(self) -> None:
        backend = get_backend("capacity_multi_set")
        async with backend(persistent=False, close_on_exit=True):
            await fill_backend_with_expiring_keys(backend)
            items = {backend.get_key(f"new{i}"): f"v{i}" for i in range(4)}
            await backend.multi_set(items, expire=30)
            for i in range(4):
                assert await backend.get(backend.get_key(f"new{i}")) == f"v{i}"

    async def test_reclaims_repeatedly_not_just_once(self) -> None:
        backend = get_backend("capacity_repeat")
        async with backend(persistent=False, close_on_exit=True):
            for round_ in range(3):
                await fill_backend_with_expiring_keys(backend)
                key = backend.get_key(f"r{round_}")
                await backend.set(key, "v", expire=30)
                assert await backend.get(key) == "v"
                await backend.delete(key)


class TestGenuinelyFull:
    async def test_full_of_live_keys_still_fails_loudly(self) -> None:
        backend = get_backend("capacity_live")
        async with backend(persistent=False, close_on_exit=True):
            for i in range(MAX_KEYS):
                await backend.set(backend.get_key(f"live{i}"), f"v{i}", expire=60)

            with pytest.raises(BackendError, match="max_keys_per_shard"):
                await backend.set(backend.get_key("overflow"), "z")

            # The reclaim attempt must not have evicted anything live.
            for i in range(MAX_KEYS):
                assert await backend.get(backend.get_key(f"live{i}")) == f"v{i}"

    async def test_mixed_live_and_expired_reclaims_only_the_expired(self) -> None:
        backend = get_backend("capacity_mixed")
        async with backend(persistent=False, close_on_exit=True):
            for i in range(MAX_KEYS // 2):
                await backend.set(backend.get_key(f"live{i}"), f"v{i}", expire=60)
            for i in range(MAX_KEYS // 2):
                await backend.set(backend.get_key(f"dead{i}"), "x", expire=1)
            await asyncio.sleep(1.2)

            for i in range(MAX_KEYS // 2):
                await backend.set(backend.get_key(f"new{i}"), "n", expire=60)

            for i in range(MAX_KEYS // 2):
                assert await backend.get(backend.get_key(f"live{i}")) == f"v{i}"
                assert await backend.get(backend.get_key(f"new{i}")) == "n"
