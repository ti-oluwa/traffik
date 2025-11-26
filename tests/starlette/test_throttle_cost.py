"""Tests for cost functionality in HTTP and WebSocket throttles."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.asyncio_client import AsyncioTestClient
from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_with_default_cost(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test HTTPThrottle with default cost."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-default-cost",
            rate="5/s",
            identifier=default_client_identifier,
        )

        async def endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "success"})

        routes = [Route("/api/endpoint", endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Make 5 requests with default cost (should all succeed)
            for i in range(5):
                response = await client.get("/api/endpoint")
                assert response.status_code == 200, f"Request {i + 1} should succeed"

            # 6th request should be throttled
            response = await client.get("/api/endpoint")
            assert response.status_code == 429, "Request 6 should be throttled"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_with_custom_cost(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test HTTPThrottle with custom cost."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-custom-cost",
            rate="10/s",
            identifier=default_client_identifier,
            cost=2,
        )

        async def expensive_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "expensive operation"})

        routes = [Route("/api/expensive", expensive_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Make 5 requests with cost=2 each (total 10)
            for i in range(5):
                response = await client.get("/api/expensive")
                assert response.status_code == 200, f"Request {i + 1} should succeed"

            # 6th request would be 12 total, should be throttled
            response = await client.get("/api/expensive")
            assert response.status_code == 429, "Request 6 should be throttled"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_override_cost_per_request(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test HTTPThrottle with per-request cost override."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-override-cost",
            rate="20/2s",
            identifier=default_client_identifier,
            cost=2,
        )

        async def light_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "light operation"})

        async def heavy_endpoint(request: Request) -> JSONResponse:
            # Override cost for this specific request
            await throttle(request, cost=5)
            return JSONResponse({"message": "heavy operation"})

        routes = [
            Route("/api/light", light_endpoint, methods=["GET"]),
            Route("/api/heavy", heavy_endpoint, methods=["GET"]),
        ]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Make 10 light requests (cost 10 total)
            for i in range(10):
                response = await client.get("/api/light")
                assert response.status_code == 200

            # Now at 20, one more light request should be throttled
            response = await client.get("/api/light")
            assert response.status_code == 429

            # Make 2 heavy requests (cost 10 total, overall 20)
            for i in range(2):
                response = await client.get("/api/heavy")
                assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_websocket_throttle_with_cost(inmemory_backend: InMemoryBackend) -> None:
    """Test WebSocketThrottle with variable costs."""
    async with inmemory_backend(close_on_exit=True):
        ws_throttle = WebSocketThrottle(
            "test-ws-cost",
            rate="10/2s",
            identifier=default_client_identifier,
            cost=2,
        )

        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            print("Accepted websocket connection")
            close_code = 1000  # Normal closure
            close_reason = "Normal closure"

            while True:
                try:
                    data = await websocket.receive_text()
                    await ws_throttle(websocket)
                    await websocket.send_text(f"Echo: {data}")
                except HTTPException as exc:
                    print("HTTPException caught in websocket:", exc)
                    await websocket.send_text("Throttled")
                    close_code = 1008  # Policy Violation
                    close_reason = exc.detail
                    break
                except Exception as exc:
                    print("Exception caught in websocket:", exc)
                    await websocket.send_text("Internal error")
                    close_code = 1011  # Internal Error
                    close_reason = "Internal error"
                    break

            await asyncio.sleep(1)  # Ensure message is sent before closing
            await websocket.close(code=close_code, reason=close_reason)

        routes = [WebSocketRoute("/ws", websocket_endpoint)]
        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with (
            AsyncioTestClient(
                app=app,
                base_url=base_url,
                event_loop=running_loop,
            ) as client,
            client.websocket_connect(url="/ws") as ws,
        ):
            # Reset the backend before starting the test
            # as connecting to the websocket already counts as a request
            # and we want to start fresh.
            await inmemory_backend.reset()
            await inmemory_backend.initialize()

            async def make_ws_request(id: int) -> tuple[str, int]:
                try:
                    await ws.send_text(f"Message {id}")
                    response = await ws.receive_text()
                    return response, 200
                except WebSocketDisconnect as exc:
                    print("WEBSOCKET DISCONNECT:", exc)
                    return "disconnected", 1000

            # Send 5 messages with cost=2 each (total 10)
            for i in range(5):
                response, code = await make_ws_request(i)
                assert response == f"Echo: Message {i}"
                assert code == 200

            # 6th message should be throttled (would be 12 total)
            response, code = await make_ws_request(6)
            assert response == "Throttled"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttle_cost_isolation(inmemory_backend: InMemoryBackend) -> None:
    """Test that costs are isolated between different throttles."""
    async with inmemory_backend(close_on_exit=True):
        throttle1 = HTTPThrottle(
            "test-isolation",
            rate="10/s",
            identifier=default_client_identifier,
            cost=2,
        )
        throttle2 = HTTPThrottle(
            "test-isolation",
            rate="10/s",
            identifier=default_client_identifier,
            cost=3,
        )

        async def endpoint1(request: Request) -> JSONResponse:
            await throttle1(request)
            return JSONResponse({"endpoint": 1})

        async def endpoint2(request: Request) -> JSONResponse:
            await throttle2(request)
            return JSONResponse({"endpoint": 2})

        routes = [
            Route("/api/endpoint1", endpoint1, methods=["GET"]),
            Route("/api/endpoint2", endpoint2, methods=["GET"]),
        ]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Use endpoint1 (cost=2, 5 times = 10)
            for _ in range(5):
                response = await client.get("/api/endpoint1")
                assert response.status_code == 200

            # endpoint1 should be throttled
            response = await client.get("/api/endpoint1")
            assert response.status_code == 429

            # But endpoint2 should still work (different path and cost)
            response = await client.get("/api/endpoint2")
            assert response.status_code == 200
