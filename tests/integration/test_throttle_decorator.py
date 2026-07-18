"""
Tests for the generic `throttled` decorator (`traffik.throttles.throttled`).

Runs against both FastAPI and Starlette via the `web_framework` fixture. This
decorator just wraps a handler and inspects its args/kwargs for an
`HTTPConnection`/`WebSocket` -- it doesn't touch FastAPI's dependency system --
so it behaves identically regardless of which framework the route is
registered on.

NOTE: this is a different object from `traffik.decorators.throttled`, which is
explicitly FastAPI-only (it wraps the throttle as a real `fastapi.params.Depends`
so it participates in FastAPI's DI graph, gets proper OpenAPI docs, etc.). That
decorator has no Starlette equivalent and is tested on its own in
`tests/integration/fastapi/test_throttle_decorator.py` -- don't confuse the two
when reading test names across both files.
"""

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.frameworks import ASGIFramework, HTTPRoute, WSRoute
from tests.utils import default_client_identifier, make_client
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, WebSocketThrottle, throttled


@pytest.mark.throttle
@pytest.mark.decorator
def test_throttled_no_throttles_raises() -> None:
    """`throttled()` with no throttles raises ValueError. No app involved."""
    with pytest.raises(ValueError, match="At least one throttle"):
        throttled()


@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.anyio
async def test_throttled_no_connection_raises() -> None:
    """Raises ValueError when no HTTPConnection/WebSocket is in the call args."""
    throttle = HTTPThrottle(
        "test-decorator-no-conn",
        rate="5/s",
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )

    @throttled(throttle)
    async def bad_endpoint(some_arg: str) -> JSONResponse:
        return JSONResponse({"message": "ok"})

    with pytest.raises(ValueError, match="No HTTP connection found"):
        await bad_endpoint("hello")  # type: ignore[arg-type]


@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.anyio
async def test_throttled_websocket_no_connection_raises() -> None:
    """Raises ValueError when no WebSocket is in the call args."""
    throttle = WebSocketThrottle(
        "test-ws-decorator-no-conn",
        rate="5/s",
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )

    @throttled(throttle)
    async def bad_ws_endpoint(some_arg: str) -> None:
        pass

    with pytest.raises(ValueError, match="No HTTP connection found"):
        await bad_ws_endpoint("hello")  # type: ignore[arg-type]


@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.anyio
async def test_throttled_preserves_function_metadata() -> None:
    """`@throttled` preserves the wrapped function's `__name__`/`__doc__`."""
    throttle = HTTPThrottle(
        "test-decorator-meta",
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


@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.anyio
class TestDecoratorWithHTTPThrottle:
    async def test_single_throttle(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """4th request in a 3/s limit is throttled."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-decorator-single-{web_framework.name}",
                rate="3/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(throttle)
            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

            async with make_client(app, base_url="http://test") as client:
                for i in range(3):
                    response = await client.get("/")
                    assert response.status_code == 200, (
                        f"Request {i + 1} should succeed"
                    )
                    assert response.json() == {"message": "ok"}

                response = await client.get("/")
                assert response.status_code == 429, "Request 4 should be throttled"

    async def test_multiple_throttles_sequential(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """The more restrictive of two stacked throttles governs the limit."""
        async with inmemory_backend(close_on_exit=True):
            burst_throttle = HTTPThrottle(
                f"test-decorator-burst-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )
            sustained_throttle = HTTPThrottle(
                f"test-decorator-sustained-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(burst_throttle, sustained_throttle)
            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

            async with make_client(app, base_url="http://test") as client:
                for i in range(2):
                    response = await client.get("/")
                    assert response.status_code == 200, (
                        f"Request {i + 1} should succeed"
                    )

                response = await client.get("/")
                assert response.status_code == 429, "Should be throttled by burst limit"

    async def test_multiple_throttles_short_circuit(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """When the first throttle blocks, the second is never reached."""
        async with inmemory_backend(close_on_exit=True):
            first_throttle = HTTPThrottle(
                f"test-decorator-first-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )
            second_throttle = HTTPThrottle(
                f"test-decorator-second-{web_framework.name}",
                rate="100/m",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(first_throttle, second_throttle)
            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    response = await client.get("/")
                    assert response.status_code == 200

                response = await client.get("/")
                assert response.status_code == 429
                assert response.headers.get("Retry-After") is not None

    async def test_with_route_parameter_direct_form(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """`throttled(throttle, route=func)` direct-call form, not as a decorator."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-decorator-route-param-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            wrapped = throttled(throttle, route=endpoint)
            app = web_framework.build_app(http_routes=[HTTPRoute("/", wrapped)])

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    response = await client.get("/")
                    assert response.status_code == 200

                response = await client.get("/")
                assert response.status_code == 429

    async def test_connection_in_kwargs(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Finds the HTTPConnection even when passed as a keyword arg."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-decorator-kwargs-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(throttle)
            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

            async with make_client(app, base_url="http://test") as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/")).status_code == 429

    async def test_different_routes_share_throttle_but_scope_by_path(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Same throttle instance on two routes -- path is part of the key."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-decorator-routes-{web_framework.name}",
                rate="3/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(throttle)
            async def route_a(request: Request) -> JSONResponse:
                return JSONResponse({"route": "a"})

            @throttled(throttle)
            async def route_b(request: Request) -> JSONResponse:
                return JSONResponse({"route": "b"})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/a", route_a),
                    HTTPRoute("/b", route_b),
                ]
            )

            async with make_client(app, base_url="http://test") as client:
                for _ in range(3):
                    assert (await client.get("/a")).status_code == 200

                assert (await client.get("/a")).status_code == 429
                assert (await client.get("/b")).status_code == 200

    async def test_retry_after_header(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """A throttled response includes a Retry-After header."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-decorator-retry-{web_framework.name}",
                rate="1/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(throttle)
            async def endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"message": "ok"})

            app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

            async with make_client(app, base_url="http://test") as client:
                assert (await client.get("/")).status_code == 200

                response = await client.get("/")
                assert response.status_code == 429
                assert response.headers.get("Retry-After") is not None


@pytest.mark.throttle
@pytest.mark.decorator
@pytest.mark.websocket
@pytest.mark.anyio
class TestDecoratorWithWebSocketThrottle:
    async def test_single_throttle(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """3rd connection in a 2/5s limit is throttled."""
        async with inmemory_backend(close_on_exit=True):
            throttle = WebSocketThrottle(
                f"test-ws-decorator-{web_framework.name}",
                rate="2/5s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(throttle)
            async def ws_endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_json({"message": "connected"})
                await websocket.close()

            app = web_framework.build_app(ws_routes=[WSRoute("/ws", ws_endpoint)])

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    async with client.websocket_connect("/ws") as ws:
                        data = await ws.receive_json()
                        assert data == {"message": "connected"}

                with pytest.raises((WebSocketDisconnect, Exception)):
                    async with client.websocket_connect("/ws") as ws:
                        await ws.receive_json()

    async def test_multiple_throttles(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Stacked throttles: the more restrictive one governs."""
        async with inmemory_backend(close_on_exit=True):
            burst = WebSocketThrottle(
                f"test-ws-burst-{web_framework.name}",
                rate="2/5s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )
            sustained = WebSocketThrottle(
                f"test-ws-sustained-{web_framework.name}",
                rate="5/60s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            @throttled(burst, sustained)
            async def ws_endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_json({"message": "ok"})
                await websocket.close()

            app = web_framework.build_app(ws_routes=[WSRoute("/ws", ws_endpoint)])

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    async with client.websocket_connect("/ws") as ws:
                        data = await ws.receive_json()
                        assert data == {"message": "ok"}

                with pytest.raises((WebSocketDisconnect, Exception)):
                    async with client.websocket_connect("/ws") as ws:
                        await ws.receive_json()

    async def test_with_route_parameter_direct_form(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """`throttled(throttle, route=func)` direct-call form for WebSocket."""
        async with inmemory_backend(close_on_exit=True):
            throttle = WebSocketThrottle(
                f"test-ws-decorator-route-param-{web_framework.name}",
                rate="2/5s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def ws_endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_json({"message": "ok"})
                await websocket.close()

            wrapped = throttled(throttle, route=ws_endpoint)
            app = web_framework.build_app(ws_routes=[WSRoute("/ws", wrapped)])

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    async with client.websocket_connect("/ws") as ws:
                        data = await ws.receive_json()
                        assert data == {"message": "ok"}

                with pytest.raises((WebSocketDisconnect, Exception)):
                    async with client.websocket_connect("/ws") as ws:
                        await ws.receive_json()

    async def test_different_routes_share_throttle_but_scope_by_path(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Same throttle instance on two WS routes -- path is part of the key."""
        async with inmemory_backend(close_on_exit=True):
            throttle = WebSocketThrottle(
                f"test-ws-decorator-routes-{web_framework.name}",
                rate="2/5s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
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

            app = web_framework.build_app(
                ws_routes=[
                    WSRoute("/ws/a", route_a),
                    WSRoute("/ws/b", route_b),
                ]
            )

            async with make_client(app, base_url="http://test") as client:
                for _ in range(2):
                    async with client.websocket_connect("/ws/a") as ws:
                        data = await ws.receive_json()
                        assert data == {"route": "a"}

                with pytest.raises((WebSocketDisconnect, Exception)):
                    async with client.websocket_connect("/ws/a") as ws:
                        await ws.receive_json()

                async with client.websocket_connect("/ws/b") as ws:
                    data = await ws.receive_json()
                    assert data == {"route": "b"}
