import os

import pytest

from traffik.backends.base import throttle_backend_ctx
from traffik.backends.redis import RedisBackend

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
async def backend() -> RedisBackend:
    return RedisBackend(connection=REDIS_URL, prefix="redis-backend")


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.redis
async def test_backend_reset(backend: RedisBackend) -> None:
    await backend.reset()
    keys = await backend.connection.keys(f"{backend.prefix}:*")
    assert not keys


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.backend
@pytest.mark.throttle
@pytest.mark.redis
async def test_get_wait_period(backend: RedisBackend) -> None:
    await backend.reset()
    async with backend():
        wait_period = await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
        assert wait_period == 0
        wait_period = await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
        assert wait_period == 0
        wait_period = await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
        assert wait_period == 0
        wait_period = await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
        assert wait_period != 0


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.redis
async def test_backend_context_management(backend: RedisBackend) -> None:
    # Test that the context variable is initialized to None
    assert throttle_backend_ctx.get() is None

    assert backend._lua_sha is None
    async with backend():
        # Test the lua script has been loaded
        assert isinstance(backend._lua_sha, str)
        # Test that the context variable is set within the context
        assert throttle_backend_ctx.get() is backend

    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.backend
@pytest.mark.redis
async def test_backend_persistence(backend: RedisBackend) -> None:
    # Test that the backend can be set to persistent
    backend.persistent = True
    assert backend.persistent

    # Add some keys to the backend
    async with backend():
        await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
    # Test that the backend persists the keys even after exiting the context
    assert len(await backend.connection.keys(f"{backend.prefix}:*")) > 0
    # Reset the backend
    await backend.reset()
    # Test that the backend can be reset
    assert len(await backend.connection.keys(f"{backend.prefix}:*")) == 0
