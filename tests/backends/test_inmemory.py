import collections.abc

import pytest

from traffik.backends.base import throttle_backend_ctx
from traffik.backends.inmemory import InMemoryBackend


@pytest.fixture(scope="function")
async def backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.native
async def test_backend_reset(backend: InMemoryBackend) -> None:
    await backend.initialize()
    assert backend.connection is not None
    await backend.reset()
    assert backend.connection is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.backend
@pytest.mark.throttle
@pytest.mark.native
async def test_get_wait_period(backend: InMemoryBackend) -> None:
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
@pytest.mark.native
async def test_backend_context_management(backend: InMemoryBackend) -> None:
    # Test that the context variable is initialized to None
    assert throttle_backend_ctx.get() is None

    assert backend.connection is None
    async with backend():
        # Test the lua script has been loaded
        assert isinstance(backend.connection, collections.abc.MutableMapping)
        # Test that the context variable is set within the context
        assert throttle_backend_ctx.get() is backend

    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.backend
@pytest.mark.native
async def test_backend_persistence(backend: InMemoryBackend) -> None:
    # Test that the backend can be set to persistent
    backend.persistent = True
    assert backend.persistent

    # Add some keys to the backend
    async with backend():
        await backend.get_wait_period(
            f"{backend.prefix}:test_key", limit=3, expires_after=5000
        )
    # Test that the backend persists the keys even after exiting the context
    assert backend.connection is not None
    assert len(backend.connection.keys()) > 0
    # Reset the backend
    await backend.reset()
    # Test that the backend can be reset
    assert backend.connection is None
