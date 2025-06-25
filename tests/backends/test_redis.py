import pytest

from fastapi_throttle.backends.base import throttle_backend_ctx
from fastapi_throttle.backends.redis import RedisBackend


@pytest.fixture(scope="function")
def backend() -> RedisBackend:
    return RedisBackend(connection="redis://127.0.0.1:6379", prefix="redis-backend")


async def test_backend_reset(backend: RedisBackend) -> None:
    await backend.reset()
    keys = await backend.connection.keys(f"{backend.prefix}:*")
    assert not keys


async def test_redis_backend_context_management(backend: RedisBackend) -> None:
    # Test that the context variable is initialized to None
    assert throttle_backend_ctx.get() is None

    assert backend._lua_sha is None
    assert backend._context_token is None
    async with backend:
        # Test the lua script has been loaded
        assert isinstance(backend._lua_sha, str)
        # Test that the context variable is set within the context
        assert throttle_backend_ctx.get() is backend
        assert backend._context_token is not None

    # Test that the context token is reset after exiting the context
    assert backend._context_token is None
    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None
