import asyncio

import pytest
from starlette.applications import Starlette

from traffik.backends.base import (
    BACKEND_STATE_KEY,
    ThrottleBackend,
    get_throttle_backend,
)


@pytest.fixture(scope="module")
def backend() -> ThrottleBackend:
    return ThrottleBackend(connection=None, namespace="test")


@pytest.mark.asyncio
@pytest.mark.backend
async def test_get_key(backend: ThrottleBackend) -> None:
    key1 = backend.get_key("test_key")
    assert isinstance(key1, str)
    assert key1.startswith(backend.namespace)


@pytest.mark.asyncio
@pytest.mark.backend
async def test_context_management(backend: ThrottleBackend) -> None:
    # Test that the context variable is initialized to None
    assert get_throttle_backend() is None

    with pytest.raises(NotImplementedError):
        async with backend():
            # Test that the context variable is set within the context
            assert get_throttle_backend() is backend

    # Test that the context variable is reset after exiting the context
    assert get_throttle_backend() is None


@pytest.mark.asyncio
@pytest.mark.backend
async def test_lifespan_management(backend: ThrottleBackend) -> None:
    """Test that backend context is properly managed through application lifespan."""
    app = Starlette()

    # Test that context is None before lifespan starts
    assert get_throttle_backend() is None

    # Simulate lifespan startup
    started = False

    async def run_lifespan() -> None:
        nonlocal started
        async with backend.lifespan(app):
            started = True
            # Context should be set during lifespan
            assert get_throttle_backend() is backend
            # Check that the backend is set in the app state
            assert getattr(app.state, BACKEND_STATE_KEY, None) is backend
            # Simulate the app running for a bit
            await asyncio.sleep(0.1)

    # Run the lifespan context
    with pytest.raises(NotImplementedError):
        # This will raise NotImplementedError from backend.initialize()
        # but we can still test the context management
        await run_lifespan()
        assert started

    # After lifespan ends, context should be reset
    assert get_throttle_backend() is None
