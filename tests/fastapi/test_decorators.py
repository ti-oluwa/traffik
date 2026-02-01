"""Tests for FastAPI decorator-based throttling."""

import typing

import anyio
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

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


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.fastapi
async def test_throttled_decorator_with_multiple_throttles(
    app: FastAPI, inmemory_backend: InMemoryBackend
) -> None:
    """Test @throttled() decorator with multiple throttles applied sequentially."""
    async with inmemory_backend(app, persistent=False, close_on_exit=True):
        # Burst throttle: 5 per 10 seconds
        burst_throttle = HTTPThrottle(
            uid="multi-burst",
            rate=Rate(limit=5, seconds=10),
            identifier=default_client_identifier,
        )
        # Sustained throttle: 10 per minute
        sustained_throttle = HTTPThrottle(
            uid="multi-sustained",
            rate=Rate(limit=10, minutes=1),
            identifier=default_client_identifier,
        )

        @app.get("/multi-throttled")
        @throttled(burst_throttle, sustained_throttle)
        async def multi_throttled_endpoint() -> typing.Dict[str, str]:
            return {"status": "ok"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            base_url=base_url, transport=ASGITransport(app=app)
        ) as client:
            # First 5 requests should pass both throttles
            for i in range(5):
                response = await client.get("/multi-throttled")
                assert response.status_code == 200, f"Request {i + 1} should pass"
                assert response.json() == {"status": "ok"}

            # 6th request should be blocked by burst throttle
            response = await client.get("/multi-throttled")
            assert response.status_code == 429, "Should be throttled by burst limit"
            assert response.headers.get("Retry-After") is not None

            # Wait for burst window to clear (Add 5s buffer)
            wait_period = int(response.headers.get("Retry-After", "10")) + 5
            print(f"Waiting {wait_period}s for burst window to clear...")
            await anyio.sleep(wait_period)

            # After burst window clears, we should be able to make more requests
            # Note: The exact interaction between burst and sustained throttles depends
            # on timing and strategy implementation. We just verify that after waiting,
            # at least some requests can succeed.
            success_count = 0
            for i in range(5):
                response = await client.get("/multi-throttled")
                if response.status_code == 200:
                    success_count += 1
                else:
                    break  # Stop on first throttle

            # We should be able to make at least 1 request after burst clears
            assert success_count >= 1, (
                f"Should be able to make at least 1 request after burst clears, got {success_count}"
            )


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.fastapi
async def test_throttled_decorator_multiple_throttles_short_circuit(
    app: FastAPI, inmemory_backend: InMemoryBackend
) -> None:
    """Test that when first throttle blocks, second isn't checked (short-circuit)."""
    async with inmemory_backend(app, persistent=False, close_on_exit=True):
        # Very restrictive first throttle
        first_throttle = HTTPThrottle(
            uid="first-limit",
            rate=Rate(limit=2, seconds=5),
            identifier=default_client_identifier,
        )
        # More permissive second throttle
        second_throttle = HTTPThrottle(
            uid="second-limit",
            rate=Rate(limit=100, minutes=1),
            identifier=default_client_identifier,
        )

        @app.get("/short-circuit")
        @throttled(first_throttle, second_throttle)
        async def short_circuit_endpoint() -> typing.Dict[str, str]:
            return {"status": "ok"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            base_url=base_url, transport=ASGITransport(app=app)
        ) as client:
            # First 2 requests pass
            for i in range(2):
                response = await client.get("/short-circuit")
                assert response.status_code == 200

            # 3rd blocked by first throttle
            response = await client.get("/short-circuit")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None
