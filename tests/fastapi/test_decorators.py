"""Tests for FastAPI decorator-based throttling."""

import typing

import anyio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from tests.utils import default_client_identifier
from traffik import strategies
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle


@pytest.fixture(scope="function")
async def app() -> FastAPI:
    app = FastAPI()
    return app


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.fastapi
async def test_throttle_decorator_only(
    app: FastAPI, inmemory_backend: InMemoryBackend
) -> None:
    async with inmemory_backend(app, persistent=True, close_on_exit=True):
        throttle = HTTPThrottle(
            uid="test-decorator",
            rate=Rate(limit=3, seconds=5),
            identifier=default_client_identifier,
        )

        @app.get("/throttled", status_code=200)
        @throttled(throttle)
        async def throttled_endpoint() -> typing.Dict[str, str]:
            return {"message": "Hello, World!"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            base_url=base_url, transport=ASGITransport(app=app)
        ) as client:
            for _ in range(3):
                response = await client.get("/throttled")
                print(response.json())
                assert response.status_code == 200
                assert response.json() == {"message": "Hello, World!"}

            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.fastapi
async def test_throttle_decorator_with_dependency(
    app: FastAPI, inmemory_backend: InMemoryBackend
) -> None:
    async with inmemory_backend(app, persistent=False, close_on_exit=True):
        burst_throttle = HTTPThrottle(
            uid="test-burst",
            rate=Rate(limit=3, seconds=5),
            identifier=default_client_identifier,
            headers={"X-RateLimit-Mode": "burst"},
            strategy=strategies.TokenBucketWithDebtStrategy(),
        )
        sustained_throttle = HTTPThrottle(
            uid="test-sustained",
            rate=Rate(limit=5, seconds=10),
            identifier=default_client_identifier,
            headers={"X-RateLimit-Mode": "sustained"},
        )

        def random_value() -> str:
            return "random_value"

        @app.get(
            "/throttled", status_code=200, dependencies=[Depends(sustained_throttle)]
        )
        @throttled(burst_throttle)
        async def throttled_endpoint(
            value: str = Depends(random_value),
        ) -> typing.Dict[str, str]:
            return {"message": value}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            base_url=base_url, transport=ASGITransport(app=app)
        ) as client:
            # Burst limit test — 3 allowed, 4th should fail
            for _ in range(3):
                response = await client.get("/throttled")
                assert response.status_code == 200
                assert response.json() == {"message": random_value()}

            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None

            # Wait for burst window to clear
            wait_period = int(response.headers.get("Retry-After")) + 1
            mode = response.headers.get("X-RateLimit-Mode", "unknown")
            print(f"Waiting {wait_period}s for {mode} window to clear...")
            await anyio.sleep(wait_period)

            # Sustained rate test — allow 1 more (total: 5 in 10s)
            response = await client.get("/throttled")
            assert response.status_code == 200

            # 6th request in sustained window should fail
            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None
