"""
Tests for `tests.test_client.AsyncTestClient` -- the test client the rest of
the suite uses to talk to ASGI apps. This isn't part of the shipped package,
but it's load-bearing enough (HTTP + WebSocket + lifespan, shared across most
of `tests/integration/`) that it deserves its own coverage rather than being
implicitly verified by whatever integration tests happen to exercise it.
"""

from contextlib import asynccontextmanager

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.client import AsyncTestClient, LifespanStartupError


@pytest.mark.anyio
class TestHTTP:
    async def test_get(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"method": "GET"})

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"method": "GET"}

    async def test_post_with_json_body(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            body = await request.json()
            return JSONResponse({"received": body})

        app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.post("/", json={"name": "Widget", "price": 9.99})
            assert response.status_code == 200
            assert response.json() == {"received": {"name": "Widget", "price": 9.99}}

    @pytest.mark.parametrize(
        "method,verb",
        [
            ("get", "GET"),
            ("post", "POST"),
            ("put", "PUT"),
            ("patch", "PATCH"),
            ("delete", "DELETE"),
            ("head", "HEAD"),
            ("options", "OPTIONS"),
        ],
    )
    async def test_all_verbs_route_through_request(
        self, method: str, verb: str
    ) -> None:
        """Every verb helper should just be `request(VERB, ...)` underneath."""
        seen_methods = []

        async def endpoint(request: Request) -> JSONResponse:
            seen_methods.append(request.method)
            return JSONResponse({"method": request.method})

        app = Starlette(
            routes=[
                Route(
                    "/",
                    endpoint,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                )
            ]
        )
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await getattr(client, method)("/")
            assert seen_methods == [verb]
            if method != "head":  # HEAD responses have no body to assert on
                assert response.status_code == 200

    async def test_query_params(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse(dict(request.query_params))

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/", params={"q": "hello", "page": "2"})
            assert response.json() == {"q": "hello", "page": "2"}

    async def test_custom_headers(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"x-custom": request.headers.get("x-custom")})

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/", headers={"x-custom": "hi"})
            assert response.json() == {"x-custom": "hi"}

    async def test_default_headers_applied(self) -> None:
        """Extra headers passed to the constructor apply to every request."""

        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "user-agent": request.headers.get("user-agent"),
                    "x-tenant": request.headers.get("x-tenant"),
                }
            )

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(
            app, base_url="http://test", headers={"x-tenant": "acme"}
        ) as client:
            response = await client.get("/")
            body = response.json()
            assert body["user-agent"] == "testclient"
            assert body["x-tenant"] == "acme"

    async def test_relative_url_resolved_against_base_url(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"path": request.url.path})

        app = Starlette(routes=[Route("/nested/path", endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/nested/path")
            assert response.json() == {"path": "/nested/path"}

    async def test_404_for_unknown_route(self) -> None:
        app = Starlette(routes=[])
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/does-not-exist")
            assert response.status_code == 404

    async def test_app_exception_raised_by_default(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            raise RuntimeError("boom")

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            with pytest.raises(RuntimeError, match="boom"):
                await client.get("/")

    async def test_app_exception_suppressed_when_disabled(self) -> None:
        async def endpoint(request: Request) -> JSONResponse:
            raise RuntimeError("boom")

        app = Starlette(routes=[Route("/", endpoint)])
        async with AsyncTestClient(
            app, base_url="http://test", raise_server_exceptions=False
        ) as client:
            response = await client.get("/")
            assert response.status_code == 500


@pytest.mark.anyio
class TestWebSocket:
    async def test_send_and_receive_json(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            data = await websocket.receive_json()
            await websocket.send_json({"echo": data})
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with (
            AsyncTestClient(app, base_url="http://test") as client,
            client.websocket_connect("/ws") as ws,
        ):
            await ws.send_json({"hello": "ws"})
            response = await ws.receive_json()
            assert response == {"echo": {"hello": "ws"}}

    async def test_send_and_receive_text(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            text = await websocket.receive_text()
            await websocket.send_text(f"echo: {text}")
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with (
            AsyncTestClient(app, base_url="http://test") as client,
            client.websocket_connect("/ws") as ws,
        ):
            await ws.send_text("hi")
            assert await ws.receive_text() == "echo: hi"

    async def test_send_and_receive_bytes(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            data = await websocket.receive_bytes()
            await websocket.send_bytes(data[::-1])
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with (
            AsyncTestClient(app, base_url="http://test") as client,
            client.websocket_connect("/ws") as ws,
        ):
            await ws.send_bytes(b"abc")
            assert await ws.receive_bytes() == b"cba"

    async def test_server_close_raises_disconnect_on_next_receive(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.close(code=4001, reason="bye")

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with (
            AsyncTestClient(app, base_url="http://test") as client,
            client.websocket_connect("/ws") as ws,
        ):
            with pytest.raises(WebSocketDisconnect) as exc_info:
                await ws.receive_text()
            assert exc_info.value.code == 4001

    async def test_reject_before_accept_raises_on_connect(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.close(code=4003)

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                async with client.websocket_connect("/ws"):
                    pass
            assert exc_info.value.code == 4003

    async def test_multiple_sequential_connections(self) -> None:
        """A fresh session per connection -- state doesn't leak between them."""
        counts = {"n": 0}

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            counts["n"] += 1
            await websocket.send_json({"count": counts["n"]})
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with AsyncTestClient(app, base_url="http://test") as client:
            for expected in (1, 2, 3):
                async with client.websocket_connect("/ws") as ws:
                    assert await ws.receive_json() == {"count": expected}

    async def test_non_ws_scheme_rejected(self) -> None:
        app = Starlette(routes=[])
        async with AsyncTestClient(app, base_url="http://test") as client:
            with pytest.raises(ValueError, match="ws/wss"):
                client.websocket_connect("http://test/ws")

    async def test_subprotocol_negotiation(self) -> None:
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept(subprotocol="chat")
            await websocket.close()

        app = Starlette(routes=[WebSocketRoute("/ws", ws_endpoint)])
        async with (
            AsyncTestClient(app, base_url="http://test") as client,
            client.websocket_connect("/ws", subprotocols=["chat", "other"]) as ws,
        ):
            assert ws.accepted_subprotocol == "chat"


@pytest.mark.anyio
class TestLifespan:
    async def test_startup_and_shutdown_run(self) -> None:
        events = []

        @asynccontextmanager
        async def lifespan(app):
            events.append("startup")
            yield
            events.append("shutdown")

        app = Starlette(routes=[], lifespan=lifespan)
        async with AsyncTestClient(app, base_url="http://test"):
            assert events == ["startup"]
        assert events == ["startup", "shutdown"]

    async def test_startup_failure_raises(self) -> None:
        @asynccontextmanager
        async def lifespan(app):
            raise RuntimeError("setup failed")
            yield  # pragma: no cover

        app = Starlette(routes=[], lifespan=lifespan)
        with pytest.raises(LifespanStartupError):
            async with AsyncTestClient(app, base_url="http://test"):
                pass  # pragma: no cover

    async def test_http_and_websocket_share_lifespan_state(self) -> None:
        """
        The whole reason for merging HTTP into this client: a value set up
        during lifespan startup should be visible to both HTTP and WS handlers,
        via the shared `app` instance both request paths run against.
        """

        @asynccontextmanager
        async def lifespan(app):
            app.state.counter = 0
            yield

        async def http_endpoint(request: Request) -> JSONResponse:
            request.app.state.counter += 1
            return JSONResponse({"counter": request.app.state.counter})

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            websocket.app.state.counter += 1
            await websocket.send_json({"counter": websocket.app.state.counter})
            await websocket.close()

        app = Starlette(
            routes=[Route("/", http_endpoint), WebSocketRoute("/ws", ws_endpoint)],
            lifespan=lifespan,
        )
        async with AsyncTestClient(app, base_url="http://test") as client:
            response = await client.get("/")
            assert response.json() == {"counter": 1}

            async with client.websocket_connect("/ws") as ws:
                assert await ws.receive_json() == {"counter": 2}

            response = await client.get("/")
            assert response.json() == {"counter": 3}
