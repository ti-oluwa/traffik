import collections.abc

import pytest

from traffik.backends.base import throttle_backend_ctx
from traffik.backends.inmemory import InMemoryBackend


@pytest.fixture(scope="function")
def backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.mark.asyncio
async def test_backend_reset(backend: InMemoryBackend) -> None:
    await backend.initialize()
    await backend.reset()
    keys = backend.connection.keys()
    assert len(keys) == 0


@pytest.mark.asyncio
async def test_get_wait_period(backend: InMemoryBackend) -> None:
    await backend.reset()
    async with backend:
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
async def test_backend_context_management(backend: InMemoryBackend) -> None:
    # Test that the context variable is initialized to None
    assert throttle_backend_ctx.get() is None

    assert backend.connection is None
    assert backend._context_token is None
    async with backend:
        # Test the lua script has been loaded
        assert isinstance(backend.connection, collections.abc.MutableMapping)
        # Test that the context variable is set within the context
        assert throttle_backend_ctx.get() is backend
        assert backend._context_token is not None

    # Test that the context token is reset after exiting the context
    assert backend._context_token is None
    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None


@pytest.mark.asyncio
async def test_backend_persistence(backend: InMemoryBackend) -> None:
    # Test that the backend can be set to persistent
    backend.persistent = True
    assert backend.persistent

    # Add some keys to the backend
    async with backend:
        await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
    # Test that the backend persists the keys even after exiting the context
    assert len(backend.connection.keys()) > 0
    # Reset the backend
    await backend.reset()
    # Test that the backend can be reset
    assert len(backend.connection.keys()) == 0
