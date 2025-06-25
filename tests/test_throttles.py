import typing
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_throttle.backends.inmemory import InMemoryBackend
from fastapi_throttle.throttles import BaseThrottle, HTTPThrottle, WebSocketThrottle


@pytest.fixture(scope="function")
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def hello() -> typing.Dict[str, str]:
        return {"msg": "Hello World"}

    return app


@pytest.fixture
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


async def test_throttle_initialization(inmemory_backend: InMemoryBackend) -> None:
    # Test that an error is raised when not backend is passed or detected
    with pytest.raises(ValueError):
        BaseThrottle()

    # Test initialization behaviour
    async with inmemory_backend:
        throttle = BaseThrottle(
            limit=2,
            milliseconds=10,
            seconds=50,
            minutes=2,
            hours=1,
            handle_throttled=lambda x: x,
        )
        time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
        assert throttle.expires_after == time_in_ms
        assert throttle.backend is inmemory_backend
        assert throttle.identifier is inmemory_backend.identifier
        # Test that provided throttle handler is used
        assert throttle.handle_throttled is not inmemory_backend.handle_throttled

    with pytest.raises(ValueError):
        BaseThrottle(limit=0)

    with pytest.raises(ValueError):
        BaseThrottle()
