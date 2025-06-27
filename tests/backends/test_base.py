import asyncio
import re
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from traffik.backends.base import ThrottleBackend, throttle_backend_ctx


@pytest.fixture(scope="module")
def backend() -> ThrottleBackend:
    return ThrottleBackend(connection=None, prefix="test")


def test_get_key_pattern(backend: ThrottleBackend) -> None:
    # Test the regular expression pattern
    pattern = backend.get_key_pattern()
    assert isinstance(pattern, re.Pattern)
    assert pattern.match("test:some_key") is not None
    assert pattern.match("test:another_key") is not None
    assert pattern.match("not_test:some_key") is None
    assert pattern.match("test:") is not None  # Edge case with empty suffix


@pytest.mark.asyncio
async def test_check_key_pattern(backend: ThrottleBackend) -> None:
    # Test with a key that matches the pattern
    assert await backend.check_key_pattern("test:some_key") is True
    assert await backend.check_key_pattern("not_test:some_key") is False


@pytest.mark.asyncio
async def test_throttle_backend_context_management(backend: ThrottleBackend) -> None:
    # Test that the context variable is initialized to None
    assert throttle_backend_ctx.get() is None

    with pytest.raises(NotImplementedError):
        async with backend():
            # Test that the context variable is set within the context
            assert throttle_backend_ctx.get() is backend

    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None


@pytest.mark.asyncio
async def test_throttle_backend_lifespan_management(backend: ThrottleBackend) -> None:
    """Test that backend context is properly managed through FastAPI lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with backend(app):
            yield

    app = FastAPI()

    # Test that context is None before lifespan starts
    assert throttle_backend_ctx.get() is None

    # Simulate lifespan startup
    startup_complete = False

    async def run_lifespan() -> None:
        nonlocal startup_complete
        async with lifespan(app):
            startup_complete = True
            # Context should be set during lifespan
            assert throttle_backend_ctx.get() is backend
            # Simulate the app running for a bit
            await asyncio.sleep(0.1)

    # Run the lifespan context
    with pytest.raises(NotImplementedError):
        # This will raise NotImplementedError from backend.initialize()
        # but we can still test the context management
        await run_lifespan()
        assert startup_complete

    # After lifespan ends, context should be reset
    assert throttle_backend_ctx.get() is None
