"""Tests for the Starlette `throttled` decorator in throttles.py."""

import pytest
from httpx2 import ASGITransport, AsyncClient

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from tests.asynctestclient import AsyncTestClient
from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, WebSocketThrottle, throttled


@pytest.mark.throttle
def test_throttled_no_throttles_raises() -> None:
    """Test that throttled() with no throttles raises ValueError."""
    with pytest.raises(ValueError, match="At least one throttle"):
        throttled()


@pytest.mark.asyncio
@pytest.mark.throttle
class TestDecoratorWithHTTPThrottle:
    async def test_throttled_single_throttle(self, backend: InMemoryBackend) -> None:
        """Test `@throttled` with a single throttle on an async route."""

        throttle = HTTPThrottle(
            "test-decorator-single-sl",
            rate="3/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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

    async def test_throttled_multiple_throttles(self, backend: InMemoryBackend) -> None:
        """Test `@throttled` with multiple throttles applied sequentially."""

        burst_throttle = HTTPThrottle(
            "test-decorator-burst-sl",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        sustained_throttle = HTTPThrottle(
            "test-decorator-sustained-sl",
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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

    async def test_throttled_with_route_parameter(
        self, backend: InMemoryBackend
    ) -> None:
        """Test throttled(throttle, route=func) direct form."""

        throttle = HTTPThrottle(
            "test-decorator-route-param-sl",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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

    async def test_throttled_no_connection_raises(
        self, backend: InMemoryBackend
    ) -> None:
        """Test that throttled raises ValueError when no HTTPConnection in params."""

        throttle = HTTPThrottle(
            "test-decorator-no-conn-sl",
            rate="5/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def bad_endpoint(some_arg: str) -> JSONResponse:
            return JSONResponse({"message": "ok"})

        with pytest.raises(ValueError, match="No HTTP connection found"):
            await bad_endpoint("hello")  # type: ignore

    async def test_throttled_preserves_function_metadata(
        self,
        backend: InMemoryBackend,
    ) -> None:
        """Test that @throttled preserves the wrapped function's metadata."""

        throttle = HTTPThrottle(
            "test-decorator-meta-sl",
            rate="5/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def my_endpoint(request: Request) -> JSONResponse:
            """My endpoint docstring."""
            return JSONResponse({"message": "ok"})

        assert my_endpoint.__name__ == "my_endpoint"
        assert my_endpoint.__doc__ == "My endpoint docstring."

    async def test_throttled_connection_in_kwargs(
        self, backend: InMemoryBackend
    ) -> None:
        """Test that throttled finds HTTPConnection in kwargs."""

        throttle = HTTPThrottle(
            "test-decorator-kwargs-sl",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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

    async def test_throttled_different_routes(self, backend: InMemoryBackend) -> None:
        """Test that throttled works correctly on different routes with same throttle."""

        throttle = HTTPThrottle(
            "test-decorator-routes-sl",
            rate="3/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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

    async def test_throttled_retry_after_header(self, backend: InMemoryBackend) -> None:
        """Test that throttled responses include Retry-After header."""

        throttle = HTTPThrottle(
            "test-decorator-retry-sl",
            rate="1/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
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


@pytest.mark.anyio
@pytest.mark.throttle
class TestDecoratorWithWebsocketThrottle:
    async def test_throttled_websocket_single_throttle(
        self, backend: InMemoryBackend
    ) -> None:
        """Test `@throttled` with a WebSocketThrottle on a WebSocket route."""
        throttle = WebSocketThrottle(
            "test-ws-decorator-sl",
            rate="2/5s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "connected"})
            await websocket.close()

        routes = [WebSocketRoute("/ws", ws_endpoint)]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        async with AsyncTestClient(app, base_url="http://test") as client:
            # First 2 connections should succeed
            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    data = await ws.receive_json()
                    assert data == {"message": "connected"}

            # 3rd connection should be throttled
            with (
                pytest.raises((WebSocketDisconnect, Exception)),
            ):
                async with client.websocket_connect("/ws") as ws:
                    await ws.receive_json()

    async def test_throttled_websocket_multiple_throttles(
        self, backend: InMemoryBackend
    ) -> None:
        """Test `@throttled` with multiple `WebSocketThrottles` applied sequentially."""
        burst = WebSocketThrottle(
            "test-ws-burst-sl",
            rate="2/5s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        sustained = WebSocketThrottle(
            "test-ws-sustained-sl",
            rate="5/60s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(burst, sustained)
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "ok"})
            await websocket.close()

        routes = [WebSocketRoute("/ws", ws_endpoint)]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        async with AsyncTestClient(app, base_url="http://test") as client:
            # First 2 connections pass (burst limit)
            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    data = await ws.receive_json()
                    assert data == {"message": "ok"}

            # 3rd connection blocked by burst throttle
            with (
                pytest.raises((WebSocketDisconnect, Exception)),
            ):
                async with client.websocket_connect("/ws") as ws:
                    await ws.receive_json()

    async def test_throttled_websocket_with_route_parameter(
        self, backend: InMemoryBackend
    ) -> None:
        """Test throttled(throttle, route=func) direct form for WebSocket."""
        throttle = WebSocketThrottle(
            "test-ws-decorator-route-param-sl",
            rate="2/5s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "ok"})
            await websocket.close()

        wrapped = throttled(throttle, route=ws_endpoint)

        routes = [WebSocketRoute("/ws", wrapped)]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        async with AsyncTestClient(app, base_url="http://test") as client:
            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    data = await ws.receive_json()
                    assert data == {"message": "ok"}

            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws") as ws:
                    await ws.receive_json()

    async def test_throttled_websocket_preserves_function_metadata(
        self, backend: InMemoryBackend
    ) -> None:
        """Test that @throttled preserves the wrapped WebSocket function's metadata."""
        throttle = WebSocketThrottle(
            "test-ws-decorator-meta-sl",
            rate="5/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def my_ws_endpoint(websocket: WebSocket) -> None:
            """My WebSocket endpoint docstring."""
            await websocket.accept()
            await websocket.close()

        assert my_ws_endpoint.__name__ == "my_ws_endpoint"
        assert my_ws_endpoint.__doc__ == "My WebSocket endpoint docstring."

    async def test_throttled_websocket_no_connection_raises(
        self, backend: InMemoryBackend
    ) -> None:
        """Test that throttled raises ValueError when no WebSocket in params."""
        throttle = WebSocketThrottle(
            "test-ws-decorator-no-conn-sl",
            rate="5/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def bad_ws_endpoint(some_arg: str) -> None:
            pass

        with pytest.raises(ValueError, match="No HTTP connection found"):
            await bad_ws_endpoint("hello")  # type: ignore

    async def test_throttled_websocket_different_routes(
        self, backend: InMemoryBackend
    ) -> None:
        """Test that throttled works correctly on different WebSocket routes with same throttle."""
        throttle = WebSocketThrottle(
            "test-ws-decorator-routes-sl",
            rate="2/5s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @throttled(throttle)
        async def route_a(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"route": "a"})
            await websocket.close()

        @throttled(throttle)
        async def route_b(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"route": "b"})
            await websocket.close()

        routes = [
            WebSocketRoute("/ws/a", route_a),
            WebSocketRoute("/ws/b", route_b),
        ]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        async with AsyncTestClient(app, base_url="http://test") as client:
            # Exhaust route A's limit
            for _ in range(2):
                async with client.websocket_connect("/ws/a") as ws:
                    data = await ws.receive_json()
                    assert data == {"route": "a"}

            # Route A should be throttled
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws/a") as ws:
                    await ws.receive_json()

            # Route B should work (different path in scoped key)
            async with client.websocket_connect("/ws/b") as ws:
                data = await ws.receive_json()
                assert data == {"route": "b"}
