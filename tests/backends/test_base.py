import re

import pytest

from traffik.backends.base import ThrottleBackend, throttle_backend_ctx


@pytest.fixture(scope="module")
def backend():
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

    assert backend._context_token is None
    with pytest.raises(NotImplementedError):
        async with backend:
            # Test that the context variable is set within the context
            assert throttle_backend_ctx.get() is backend
            assert backend._context_token is not None

    # Test that the context token is reset after exiting the context
    assert backend._context_token is None
    # Test that the context variable is reset after exiting the context
    assert throttle_backend_ctx.get() is None
