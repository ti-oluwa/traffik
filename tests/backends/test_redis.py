import os

import pytest
from redis.asyncio import Redis

from traffik.backends.redis import RedisBackend

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
async def backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, namespace="redis-backend")


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.redis
async def test_persistence(backend: RedisBackend) -> None:
    # Test that the backend can be set to persistent
    backend.persistent = True
    assert backend.persistent

    # Add some keys to the backend within the context
    async with backend(close_on_exit=False):
        key = await backend.get_key("test_key")
        await backend.set(key, "test_value")

    # Test that the backend persists the keys even after exiting the context
    # Connection should still be available
    key = await backend.get_key("test_key")
    value = await backend.get(key)
    assert value == "test_value"

    # Test that the backend can be reset
    await backend.reset()
    value = await backend.get(key)
    assert value is None

    # Cleanup
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.backend
@pytest.mark.redis
async def test_reset(backend: RedisBackend) -> None:
    # Initialize and add some data
    async with backend():
        key = await backend.get_key("test_key")
        await backend.set(key, "test_value")

        # Verify data exists
        value = await backend.get(key)
        assert value == "test_value"
        # Reset while in context
        await backend.reset()
        # Verify data is cleared
        value = await backend.get(key)
        assert value is None
