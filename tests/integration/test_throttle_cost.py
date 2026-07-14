"""
Tests for cost functionality in HTTP and WebSocket throttles.

Runs against both FastAPI and Starlette via the `web_framework` fixture. The
throttle logic under test (`await throttle(connection)`) is identical on both
stacks -- only route registration differs -- so one test body covers both.
"""

import asyncio
import typing

import pytest
from starlette.exceptions import WebSocketException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.frameworks import (
    ASGIFramework,
    HTTPRoute,
    WSRoute,
)
from tests.utils import default_client_identifier, make_client
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, WebSocketThrottle, is_throttled


@pytest.mark.throttle
@pytest.mark.anyio
class TestHTTPThrottleCost:
    async def test_default_cost(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Each request costs 1 by default; the 6th of a 5/s limit is throttled."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-default-cost-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )

            async def endpoint(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"message": "success"})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/endpoint", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                for i in range(5):
                    response = await client.get("/api/endpoint")
                    assert response.status_code == 200, (
                        f"Request {i + 1} should succeed"
                    )

                response = await client.get("/api/endpoint")
                assert response.status_code == 429, "Request 6 should be throttled"

    async def test_custom_cost(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """cost=2 against a 10/s limit throttles on the 6th request (would be 12)."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-custom-cost-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                cost=2,
                registry=ThrottleRegistry(),
            )

            async def endpoint(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"message": "expensive operation"})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/expensive", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                for i in range(5):
                    response = await client.get("/api/expensive")
                    assert response.status_code == 200, (
                        f"Request {i + 1} should succeed"
                    )

                response = await client.get("/api/expensive")
                assert response.status_code == 429, "Request 6 should be throttled"

    async def test_override_cost_per_request(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """`await throttle(request, cost=N)` overrides the throttle's default cost."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-override-cost-{web_framework.name}",
                rate="20/2s",
                identifier=default_client_identifier,
                cost=2,
                registry=ThrottleRegistry(),
            )

            async def light_endpoint(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"message": "light operation"})

            async def heavy_endpoint(request: Request) -> JSONResponse:
                await throttle(request, cost=5)
                return JSONResponse({"message": "heavy operation"})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/light", light_endpoint),
                    HTTPRoute("/api/heavy", heavy_endpoint),
                ]
            )

            async with make_client(app, base_url="http://test") as client:
                # 10 light requests (cost 10 total)
                for _ in range(10):
                    response = await client.get("/api/light")
                    assert response.status_code == 200

                # Now at 20, one more light request should be throttled
                response = await client.get("/api/light")
                assert response.status_code == 429

                # 2 heavy requests (cost 10 total, overall 20)
                for _ in range(2):
                    response = await client.get("/api/heavy")
                    assert response.status_code == 200

    async def test_cost_isolation_between_throttles(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Costs on one throttle don't bleed into another throttle's budget."""
        async with inmemory_backend(close_on_exit=True):
            throttle1 = HTTPThrottle(
                f"test-isolation-1-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                cost=2,
                registry=ThrottleRegistry(),
            )
            throttle2 = HTTPThrottle(
                f"test-isolation-2-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                cost=3,
                registry=ThrottleRegistry(),
            )

            async def endpoint1(request: Request) -> JSONResponse:
                await throttle1(request)
                return JSONResponse({"endpoint": 1})

            async def endpoint2(request: Request) -> JSONResponse:
                await throttle2(request)
                return JSONResponse({"endpoint": 2})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/endpoint1", endpoint1),
                    HTTPRoute("/api/endpoint2", endpoint2),
                ]
            )

            async with make_client(app, base_url="http://test") as client:
                # endpoint1 (cost=2, 5 times = 10)
                for _ in range(5):
                    response = await client.get("/api/endpoint1")
                    assert response.status_code == 200

                response = await client.get("/api/endpoint1")
                assert response.status_code == 429

                # endpoint2 is unaffected (different throttle, different cost)
                response = await client.get("/api/endpoint2")
                assert response.status_code == 200


@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.anyio
class TestWebSocketThrottleCost:
    async def test_cost_per_message(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """cost=2 per message against a 10/2s limit throttles on the 6th message."""
        async with inmemory_backend(close_on_exit=True):

            async def ws_throttled(
                connection: WebSocket,
                wait_ms: float,
                throttle: WebSocketThrottle,
                context: typing.Mapping[str, typing.Any],
            ) -> None:
                await connection.send_text("Throttled")
                await asyncio.sleep(0.1)  # let the message flush before closing
                await connection.close(code=1008, reason="Throttled")

            ws_throttle = WebSocketThrottle(
                f"test-ws-cost-{web_framework.name}",
                rate="10/2s",
                identifier=default_client_identifier,
                cost=2,
                handle_throttled=ws_throttled,
                registry=ThrottleRegistry(),
            )

            async def endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                while True:
                    try:
                        data = await websocket.receive_text()
                        await ws_throttle(websocket)
                        if is_throttled(websocket):
                            break
                        await websocket.send_text(f"Echo: {data}")
                    except WebSocketDisconnect:
                        # Throttle handler may have already closed the connection
                        break
                    except WebSocketException:
                        if websocket.client_state.value != 3:  # 3 == DISCONNECTED
                            try:
                                await websocket.send_text("Internal error")
                            except WebSocketException:
                                pass
                        break

            app = web_framework.build_app(ws_routes=[WSRoute("/ws", endpoint)])

            base_url = "http://0.0.0.0"
            running_loop = asyncio.get_running_loop()
            async with (
                make_client(app, base_url=base_url, loop=running_loop) as client,
                client.websocket_connect(url="/ws") as ws,
            ):
                # Connecting already counts as a request against the backend,
                # so reset before asserting on a clean budget.
                await inmemory_backend.reset()
                await inmemory_backend.initialize()

                async def make_ws_request(msg_id: int) -> typing.Tuple[str, int]:
                    try:
                        await ws.send_text(f"Message {msg_id}")
                        response = await ws.receive_text()
                        return response, 200
                    except WebSocketDisconnect:
                        return "disconnected", 1000

                # 5 messages with cost=2 each (total 10)
                for i in range(5):
                    response, code = await make_ws_request(i)
                    assert response == f"Echo: Message {i}"
                    assert code == 200

                # 6th message would be 12 total -- throttled
                response, _code = await make_ws_request(6)
                assert response == "Throttled"
