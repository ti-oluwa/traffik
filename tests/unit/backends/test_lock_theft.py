"""Regression tests for lock-theft protection."""

import asyncio
import os

import pytest

from tests.conftest import MEMCACHED_HOST, MEMCACHED_PORT, BackendGen, SkipBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.memcached.aiomcache import MemcachedBackend as AioMcacheBackend
from traffik.backends.memcached.emcache import MemcachedBackend as EmcacheBackend
from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.exceptions import LockReleaseError

# Bounded so a crashed/killed test run can't leave a permanent key behind to
# poison a later run against the same server.
INJECTED_KEY_TTL = 30


def require_memcached() -> None:
    if os.getenv("SKIP_MEMCACHED_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend(
            "Skipping Memcached backend tests as per environment setting."
        )


@pytest.mark.anyio
@pytest.mark.backend
class TestAiomcacheLockTheftProtection:
    async def test_release_does_not_delete_a_reacquired_lock(self) -> None:
        """
        Inject a re-acquisition exactly between the ownership read and the CAS.
        """
        require_memcached()
        backend = AioMcacheBackend(
            host=MEMCACHED_HOST,
            port=MEMCACHED_PORT,
            namespace="lock-theft-aiomcache",
            persistent=False,
            track_keys=True,
        )
        async with backend(close_on_exit=True):
            key_name = backend.get_key("shared")
            lock = backend.get_lock(key_name)
            await lock.acquire()
            inner = lock._lock  # unwrap the `_GatedNamedLock` proxy
            name_bytes = inner._name_bytes
            client = inner._client
            real_gets = client.gets

            async def gets_then_simulate_reacquisition(*args, **kwargs):
                result = await real_gets(*args, **kwargs)
                # Someone else's lock expiring and being re-acquired,
                # happening right after our ownership read.
                await client.delete(name_bytes)
                await client.add(
                    name_bytes, b"other-holder-token", exptime=INJECTED_KEY_TTL
                )
                return result

            client.gets = gets_then_simulate_reacquisition
            try:
                await lock.release()
            finally:
                client.gets = real_gets

            current = await client.get(name_bytes)
            assert current == b"other-holder-token"
            await client.delete(name_bytes)  # cleanup

    async def test_release_still_deletes_when_uncontended(self) -> None:
        """Sanity check: the fix must not break the ordinary, uncontended
        release path.
        """
        require_memcached()
        backend = AioMcacheBackend(
            host=MEMCACHED_HOST,
            port=MEMCACHED_PORT,
            namespace="lock-theft-aiomcache-normal",
            persistent=False,
            track_keys=True,
        )
        async with backend(close_on_exit=True):
            async with backend.lock("shared"):
                pass
            # Re-acquiring immediately proves the key was actually deleted.
            async with backend.lock("shared"):
                pass


@pytest.mark.anyio
@pytest.mark.backend
class TestEmcacheLockTheftProtection:
    async def test_release_does_not_delete_a_reacquired_lock(self) -> None:
        require_memcached()
        backend = EmcacheBackend(
            host=MEMCACHED_HOST,
            port=MEMCACHED_PORT,
            namespace="lock-theft-emcache",
            persistent=False,
            track_keys=True,
        )
        async with backend(close_on_exit=True):
            key_name = backend.get_key("shared")
            lock = backend.get_lock(key_name)
            await lock.acquire()
            inner = lock._lock
            name_bytes = inner._name_bytes
            client = inner._client
            real_gets = client.gets

            async def gets_then_simulate_reacquisition(*args, **kwargs):
                result = await real_gets(*args, **kwargs)
                await client.delete(name_bytes)
                await client.add(
                    name_bytes, b"other-holder-token", exptime=INJECTED_KEY_TTL
                )
                return result

            client.gets = gets_then_simulate_reacquisition
            try:
                await lock.release()
            finally:
                client.gets = real_gets

            current = await client.get(name_bytes)
            assert current is not None
            assert current.value == b"other-holder-token"
            await client.delete(name_bytes)  # cleanup

    async def test_release_still_deletes_when_uncontended(self) -> None:
        require_memcached()
        backend = EmcacheBackend(
            host=MEMCACHED_HOST,
            port=MEMCACHED_PORT,
            namespace="lock-theft-emcache-normal",
            persistent=False,
            track_keys=True,
        )
        async with backend(close_on_exit=True):
            async with backend.lock("shared"):
                pass
            async with backend.lock("shared"):
                pass


@pytest.mark.anyio
@pytest.mark.backend
class TestRedisLockTheftProtection:
    """
    The default Redis lock (`_AsyncRedisLock`) already uses a single
    atomic Lua script (`GET` + `DEL` in one round-trip) for release, so it
    has no equivalent TOCTOU window. This just pins that guarantee.
    """

    async def test_release_does_not_delete_a_reacquired_lock(
        self, backends: BackendGen
    ) -> None:
        for backend in backends(
            namespace="lock_theft_redis",
            exclude=(InMemoryBackend, MultiProcessInMemoryBackend),
        ):
            async with backend(persistent=False, close_on_exit=True):
                key_name = backend.get_key("shared")
                lock = backend.get_lock(key_name, ttl=0.2)
                await lock.acquire(blocking=True, blocking_timeout=None)

                await asyncio.sleep(0.3)  # Let the server-side TTL expire

                other_lock = backend.get_lock(key_name, ttl=30)
                reacquired = await other_lock.acquire(
                    blocking=True, blocking_timeout=None
                )
                assert reacquired, (
                    f"{backend.__class__.__name__}: expected re-acquisition "
                    "to succeed once the original lock's TTL expired"
                )

                # The original holder, unaware its lock already expired, now
                # tries to release. Backends legitimately differ here as some
                # (aioredis's custom lock) just log a warning and return;
                # others (coredis, via its underlying library's own lock)
                # raise `LockReleaseError` to signal the same condition. Both
                # are acceptable outcomes for a stale release. What must
                # hold regardless is that the new holder's lock survives.
                try:
                    await lock.release()
                except LockReleaseError:
                    pass

                # The new holder must still be able to release its own lock
                # without a `LockReleaseError` (proving the key wasn't
                # deleted or corrupted by the stale releaser).
                await other_lock.release()
