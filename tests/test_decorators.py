import anyio
import typing

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.throttles import HTTPThrottle


@pytest.fixture(scope="function")
async def app() -> FastAPI:
    app = FastAPI()
    return app


@pytest.fixture(scope="function")
async def throttle_backend() -> InMemoryBackend:
    return InMemoryBackend(prefix="test", persistent=False)


@pytest.mark.anyio
async def test_throttle_decorator_only(
    app: FastAPI, throttle_backend: InMemoryBackend
) -> None:
    async with throttle_backend:
        throttle = HTTPThrottle(limit=3, seconds=5)

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
                assert response.status_code == 200
                assert response.json() == {"message": "Hello, World!"}

            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None


@pytest.mark.anyio
async def test_throttle_decorator_with_dependency(
    app: FastAPI, throttle_backend: InMemoryBackend
) -> None:
    async with throttle_backend:
        burst_throttle = HTTPThrottle(limit=3, seconds=5)
        sustained_throttle = HTTPThrottle(limit=5, seconds=10)

        def random_value() -> str:
            return "random_value"

        @app.get(
            "/throttled",
            status_code=200,
            dependencies=[Depends(sustained_throttle)],
        )
        @throttled(burst_throttle)
        async def throttled_endpoint(
            random_value: str = Depends(random_value),
        ) -> typing.Dict[str, str]:
            return {"message": random_value}

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
            await anyio.sleep(int(response.headers.get("Retry-After")))

            # Sustained rate test — allow 1 more (total: 5 in 10s)
            response = await client.get("/throttled")
            assert response.status_code == 200

            # 6th request in sustained window should fail
            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None
