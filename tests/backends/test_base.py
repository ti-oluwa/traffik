import asyncio
import re

import pytest
from starlette.applications import Starlette

from traffik.backends.base import (
    BACKEND_STATE_KEY,
    ThrottleBackend,
    get_throttle_backend,
)


@pytest.fixture(scope="module")
def backend() -> ThrottleBackend:
    return ThrottleBackend(connection=None, prefix="test")


@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.native
def test_get_key_pattern(backend: ThrottleBackend) -> None:
    # Test the regular expression pattern
    pattern = backend.get_key_pattern()
    assert isinstance(pattern, re.Pattern)
    assert pattern.match("test:some_key") is not None
    assert pattern.match("test:another_key") is not None
    assert pattern.match("not_test:some_key") is None
    assert pattern.match("test:") is not None  # Edge case with empty suffix


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.native
async def test_check_key_pattern(backend: ThrottleBackend) -> None:
    # Test with a key that matches the pattern
    assert await backend.check_key_pattern("test:some_key") is True
    assert await backend.check_key_pattern("not_test:some_key") is False


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.backend
@pytest.mark.native
async def test_throttle_backend_context_management(backend: ThrottleBackend) -> None:
    # Test that the context variable is initialized to None
    assert get_throttle_backend() is None

    with pytest.raises(NotImplementedError):
        async with backend():
            # Test that the context variable is set within the context
            assert get_throttle_backend() is backend

    # Test that the context variable is reset after exiting the context
    assert get_throttle_backend() is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.backend
@pytest.mark.native
async def test_throttle_backend_lifespan_management(backend: ThrottleBackend) -> None:
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
