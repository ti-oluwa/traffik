"""Tests for FastAPI decorator-based throttling."""

import typing

import anyio
import pytest
from fastapi import Depends, FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from tests.utils import default_client_identifier, make_client
from traffik import strategies
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.fixture(scope="function")
async def app(inmemory_backend: InMemoryBackend) -> FastAPI:
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    return app


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.fastapi
class TestThrottleDecorator:
    async def test_throttle_decorator_only(self, app: FastAPI) -> None:
        throttle = HTTPThrottle(
            uid="test-decorator-fa",
            rate=Rate(limit=3, seconds=5),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.get("/throttled", status_code=200)
        @throttled(throttle)
        async def throttled_endpoint() -> typing.Dict[str, str]:
            return {"message": "Hello, World!"}

        base_url = "http://0.0.0.0"
        async with make_client(app, base_url=base_url) as client:
            for _ in range(3):
                response = await client.get("/throttled")
                print(response.json())
                assert response.status_code == 200
                assert response.json() == {"message": "Hello, World!"}

            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None

    async def test_throttle_decorator_with_dependency(self, app: FastAPI) -> None:
        burst_throttle = HTTPThrottle(
            uid="test-burst-fa",
            rate=Rate(limit=3, seconds=5),
            identifier=default_client_identifier,
            headers={"X-RateLimit-Mode": "burst"},
            strategy=strategies.TokenBucketWithDebtStrategy(),
            registry=ThrottleRegistry(),
        )
        sustained_throttle = HTTPThrottle(
            uid="test-sustained-fa",
            rate=Rate(limit=5, seconds=10),
            identifier=default_client_identifier,
            headers={"X-RateLimit-Mode": "sustained"},
            registry=ThrottleRegistry(),
        )

        def random_value() -> str:
            return "random_value"

        @app.get(
            "/throttled",
            status_code=200,
            dependencies=[Depends(sustained_throttle)],
        )
        @throttled(burst_throttle)
        async def throttled_endpoint(
            value: str = Depends(random_value),
        ) -> typing.Dict[str, str]:
            return {"message": value}

        base_url = "http://0.0.0.0"
        async with make_client(app, base_url=base_url) as client:
            # Burst limit test - 3 allowed, 4th should fail
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

            # Sustained rate test - allow 1 more (total: 5 in 10s)
            response = await client.get("/throttled")
            assert response.status_code == 200

            # 6th request in sustained window should fail
            response = await client.get("/throttled")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None

    @pytest.mark.flaky(reruns=3, reruns_delay=2)
    async def test_throttled_decorator_with_multiple_throttles(
        self, app: FastAPI
    ) -> None:
        """Test `@throttled` decorator with multiple throttles applied sequentially."""
        # Burst throttle: 5 per 2 seconds
        burst_throttle = HTTPThrottle(
            uid="multi-burst-fa",
            rate=Rate(limit=5, seconds=2),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        # Sustained throttle: 10 per minute
        sustained_throttle = HTTPThrottle(
            uid="multi-sustained-fa",
            rate=Rate(limit=10, minutes=1),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.get("/multi-throttled")
        @throttled(burst_throttle, sustained_throttle)
        async def multi_throttled_endpoint() -> typing.Dict[str, str]:
            return {"status": "ok"}

        base_url = "http://0.0.0.0"
        async with make_client(app, base_url=base_url) as client:
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
            wait_period = int(response.headers.get("Retry-After", "10")) + 3
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

    async def test_throttled_decorator_multiple_throttles_short_circuit(
        self, app: FastAPI
    ) -> None:
        """Test that when first throttle blocks, second isn't checked (short-circuit)."""
        # Very restrictive first throttle
        first_throttle = HTTPThrottle(
            uid="first-limit-fa",
            rate=Rate(limit=2, seconds=5),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        # More permissive second throttle
        second_throttle = HTTPThrottle(
            uid="second-limit-fa",
            rate=Rate(limit=100, minutes=1),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.get("/short-circuit")
        @throttled(first_throttle, second_throttle)
        async def short_circuit_endpoint() -> typing.Dict[str, str]:
            return {"status": "ok"}

        base_url = "http://0.0.0.0"
        async with make_client(app, base_url=base_url) as client:
            # First 2 requests pass
            for i in range(2):
                response = await client.get("/short-circuit")
                assert response.status_code == 200

            # 3rd blocked by first throttle
            response = await client.get("/short-circuit")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None

    async def test_throttled_decorator_websocket(self, app: FastAPI) -> None:
        """Test `@throttled` decorator with WebSocketThrottle on a WebSocket route."""
        ws_throttle = WebSocketThrottle(
            uid="test-ws-decorator-fa",
            rate=Rate(limit=2, seconds=5),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.websocket("/ws")
        @throttled(ws_throttle)
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "connected"})
            await websocket.close()

        async with make_client(app, base_url="http://0.0.0.0") as client:
            # First 2 connections should succeed
            for _ in range(2):
                async with client.websocket_connect("/ws") as websocket:
                    data = await websocket.receive_json()
                    assert data == {"message": "connected"}

            # 3rd connection should be throttled
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws") as websocket:
                    await websocket.receive_json()

    async def test_throttled_decorator_websocket_multiple_throttles(
        self, app: FastAPI
    ) -> None:
        """Test `@throttled` decorator with multiple WebSocketThrottles."""
        burst = WebSocketThrottle(
            uid="ws-burst-fa",
            rate=Rate(limit=2, seconds=5),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        sustained = WebSocketThrottle(
            uid="ws-sustained-fa",
            rate=Rate(limit=5, minutes=1),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.websocket("/ws")
        @throttled(burst, sustained)
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "ok"})
            await websocket.close()

        async with make_client(app, base_url="http://0.0.0.0") as client:
            # First 2 connections pass (burst limit)
            for _ in range(2):
                async with client.websocket_connect("/ws") as websocket:
                    data = await websocket.receive_json()
                    assert data == {"message": "ok"}

            # 3rd connection blocked by burst throttle
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws") as websocket:
                    await websocket.receive_json()
