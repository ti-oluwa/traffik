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
