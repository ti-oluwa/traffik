"""Tests for the Starlette `throttled` decorator in throttles.py."""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle, WebSocketThrottle, throttled


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_single_throttle(inmemory_backend: InMemoryBackend) -> None:
    """Test `@throttled` with a single throttle on an async route."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-single",
            rate="3/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        routes = [Route("/", endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First 3 requests should succeed
            for i in range(3):
                response = await client.get("/")
                assert response.status_code == 200, f"Request {i + 1} should succeed"
                assert response.json() == {"message": "ok"}

            # 4th request should be throttled
            response = await client.get("/")
            assert response.status_code == 429, "Request 4 should be throttled"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_multiple_throttles(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `@throttled` with multiple throttles applied sequentially."""
    async with inmemory_backend(close_on_exit=True):
        burst_throttle = HTTPThrottle(
            "test-decorator-burst",
            rate="2/s",
            identifier=default_client_identifier,
        )
        sustained_throttle = HTTPThrottle(
            "test-decorator-sustained",
            rate="10/s",
            identifier=default_client_identifier,
        )

        @throttled(burst_throttle, sustained_throttle)
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        routes = [Route("/", endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First 2 requests should succeed (burst limit)
            for i in range(2):
                response = await client.get("/")
                assert response.status_code == 200, f"Request {i + 1} should succeed"

            # 3rd request should be throttled by burst throttle
            response = await client.get("/")
            assert response.status_code == 429, "Should be throttled by burst limit"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_with_route_parameter(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test throttled(throttle, route=func) direct form."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-route-param",
            rate="2/s",
            identifier=default_client_identifier,
        )

        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        wrapped = throttled(throttle, route=endpoint)

        routes = [Route("/", wrapped, methods=["GET"])]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(2):
                response = await client.get("/")
                assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 429


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_no_connection_raises(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that throttled raises ValueError when no HTTPConnection in params."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-no-conn",
            rate="5/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def bad_endpoint(some_arg: str) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        with pytest.raises(ValueError, match="No HTTP connection found"):
            await bad_endpoint("hello")  # type: ignore


@pytest.mark.throttle
def test_throttled_no_throttles_raises() -> None:
    """Test that throttled() with no throttles raises ValueError."""
    with pytest.raises(ValueError, match="At least one throttle"):
        throttled()


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_preserves_function_metadata(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that @throttled preserves the wrapped function's metadata."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-meta",
            rate="5/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def my_endpoint(request: Request) -> JSONResponse:
            """My endpoint docstring."""
            return JSONResponse({"message": "ok"})

        assert my_endpoint.__name__ == "my_endpoint"
        assert my_endpoint.__doc__ == "My endpoint docstring."


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_connection_in_kwargs(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that throttled finds HTTPConnection in kwargs."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-kwargs",
            rate="2/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        routes = [Route("/", endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 429


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_different_routes(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that throttled works correctly on different routes with same throttle."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-routes",
            rate="3/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def route_a(request: Request) -> JSONResponse:
            return JSONResponse({"route": "a"})

        @throttled(throttle)
        async def route_b(request: Request) -> JSONResponse:
            return JSONResponse({"route": "b"})

        routes = [
            Route("/a", route_a, methods=["GET"]),
            Route("/b", route_b, methods=["GET"]),
        ]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Use up limit on route A
            for _ in range(3):
                response = await client.get("/a")
                assert response.status_code == 200

            # Route A should be throttled
            response = await client.get("/a")
            assert response.status_code == 429

            # Route B should work (different path in scoped key)
            response = await client.get("/b")
            assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttled_retry_after_header(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that throttled responses include Retry-After header."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-decorator-retry",
            rate="1/s",
            identifier=default_client_identifier,
        )

        @throttled(throttle)
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        routes = [Route("/", endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 429
            assert response.headers.get("Retry-After") is not None


@pytest.mark.throttle
def test_throttled_websocket_single_throttle(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `@throttled` with a WebSocketThrottle on a WebSocket route."""
    throttle = WebSocketThrottle(
        "test-ws-decorator",
        rate="2/5s",
        identifier=default_client_identifier,
    )

    @throttled(throttle)
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"message": "connected"})
        await websocket.close()

    routes = [WebSocketRoute("/ws", ws_endpoint)]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://test") as client:
        # First 2 connections should succeed
        for _ in range(2):
            with client.websocket_connect("/ws") as websocket:
                data = websocket.receive_json()
                assert data == {"message": "connected"}

        # 3rd connection should be throttled
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()


@pytest.mark.throttle
def test_throttled_websocket_multiple_throttles(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `@throttled` with multiple `WebSocketThrottles` applied sequentially."""
    burst = WebSocketThrottle(
        "test-ws-burst",
        rate="2/5s",
        identifier=default_client_identifier,
    )
    sustained = WebSocketThrottle(
        "test-ws-sustained",
        rate="5/60s",
        identifier=default_client_identifier,
    )

    @throttled(burst, sustained)
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"message": "ok"})
        await websocket.close()

    routes = [WebSocketRoute("/ws", ws_endpoint)]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://test") as client:
        # First 2 connections pass (burst limit)
        for _ in range(2):
            with client.websocket_connect("/ws") as websocket:
                data = websocket.receive_json()
                assert data == {"message": "ok"}

        # 3rd connection blocked by burst throttle
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()
