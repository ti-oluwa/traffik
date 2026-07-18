"""
Tests for stat functionality in HTTP and WebSocket throttles.
"""

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from tests.frameworks import ASGIFramework, HTTPRoute, WSRoute
from tests.utils import default_client_identifier, make_client
from traffik import connection_throttled
from traffik.backends.inmemory import InMemoryBackend
from traffik.exceptions import ConnectionThrottled
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.mark.throttle
@pytest.mark.anyio
class TestHTTPThrottleStat:
    async def test_stat_basic(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() reflects hits_remaining/wait_ms before and after a hit."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-stat-basic-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat_before = await throttle.stat(request)
                await throttle(request)
                stat_after = await throttle.stat(request)
                return JSONResponse(
                    {
                        "before": {
                            "hits_remaining": stat_before.hits_remaining
                            if stat_before
                            else None,
                            "wait_ms": stat_before.wait_ms if stat_before else None,
                        },
                        "after": {
                            "hits_remaining": stat_after.hits_remaining
                            if stat_after
                            else None,
                            "wait_ms": stat_after.wait_ms if stat_after else None,
                        },
                    }
                )

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/data", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                response = await client.get("/api/data")
                assert response.status_code == 200
                data = response.json()

                assert data["before"]["hits_remaining"] == 10
                assert data["before"]["wait_ms"] == 0.0
                assert data["after"]["hits_remaining"] == 9
                assert data["after"]["wait_ms"] == 0.0

    async def test_stat_with_cost(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() reflects a non-default cost."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-stat-cost-{web_framework.name}",
                rate="20/s",
                identifier=default_client_identifier,
                cost=5,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat_before = await throttle.stat(request)
                await throttle(request)
                stat_after = await throttle.stat(request)
                return JSONResponse(
                    {
                        "before": stat_before.hits_remaining if stat_before else None,
                        "after": stat_after.hits_remaining if stat_after else None,
                    }
                )

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/expensive", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                response = await client.get("/api/expensive")
                data = response.json()
                assert data["before"] == 20
                assert data["after"] == 15

                response = await client.get("/api/expensive")
                data = response.json()
                assert data["before"] == 15
                assert data["after"] == 10

    async def test_stat_at_limit(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() shows 0 remaining and a positive wait once the limit is hit."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-stat-limit-{web_framework.name}",
                rate="3/s",
                identifier=default_client_identifier,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                try:
                    await throttle(request)
                    throttled = False
                except ConnectionThrottled:
                    throttled = True
                stat = await throttle.stat(request)
                return JSONResponse(
                    {
                        "hits_remaining": stat.hits_remaining if stat else None,
                        "wait_ms": stat.wait_ms if stat else None,
                        "throttled": throttled,
                    }
                )

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/limited", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                for i in range(3):
                    response = await client.get("/api/limited")
                    assert response.status_code == 200
                    data = response.json()
                    assert data["hits_remaining"] == 3 - (i + 1)
                    assert data["throttled"] is False

                response = await client.get("/api/limited")
                assert response.status_code == 200
                data = response.json()
                assert data["hits_remaining"] == 0
                assert data["wait_ms"] > 0.0
                assert data["throttled"] is True

    async def test_stat_different_keys(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() is tracked independently per identifier."""
        async with inmemory_backend(close_on_exit=True):

            async def custom_identifier(connection: Request) -> str:
                return connection.query_params.get("user", "anonymous")

            throttle = HTTPThrottle(
                f"test-stat-keys-{web_framework.name}",
                rate="5/s",
                identifier=custom_identifier,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat = await throttle.stat(request)
                await throttle(request)
                return JSONResponse(
                    {
                        "user": request.query_params.get("user"),
                        "hits_remaining": stat.hits_remaining if stat else None,
                    }
                )

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/resource", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                response = await client.get("/api/resource?user=user1")
                assert response.json()["hits_remaining"] == 5

                response = await client.get("/api/resource?user=user1")
                assert response.json()["hits_remaining"] == 4

                # User 2 has an independent budget
                response = await client.get("/api/resource?user=user2")
                assert response.json()["hits_remaining"] == 5

                # User 1's budget is unaffected by user 2's requests
                response = await client.get("/api/resource?user=user1")
                assert response.json()["hits_remaining"] == 3

    async def test_stat_unlimited_rate(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() reports infinite hits_remaining for an unlimited rate."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-stat-unlimited-{web_framework.name}",
                rate=Rate(limit=0, seconds=0),
                identifier=default_client_identifier,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat = await throttle.stat(request)
                await throttle(request)
                return JSONResponse(
                    {
                        "is_infinite": (
                            str(stat.hits_remaining) == "inf" if stat else False
                        ),
                    }
                )

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/unlimited", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                for _ in range(100):
                    response = await client.get("/api/unlimited")
                    data = response.json()
                    assert data["is_infinite"], "Should always show infinite hits"

    async def test_stat_without_strategy_support(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() returns None when the configured strategy has no get_stat."""
        async with inmemory_backend(close_on_exit=True):

            async def simple_strategy(key, rate, backend, cost=1):
                return 0.0  # Always allow, no get_stat method

            throttle = HTTPThrottle(
                f"test-no-stat-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                strategy=simple_strategy,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat = await throttle.stat(request)
                return JSONResponse({"stat": stat})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/no-stat", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                response = await client.get("/api/no-stat")
                data = response.json()
                assert data["stat"] is None

    async def test_stat_fields(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() returns all documented fields with the right types."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                f"test-stat-fields-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                strategy=FixedWindowStrategy(),
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(request: Request) -> JSONResponse:
                stat = await throttle.stat(request)
                if stat:
                    return JSONResponse(
                        {
                            "has_key": hasattr(stat, "key"),
                            "has_rate": hasattr(stat, "rate"),
                            "has_hits_remaining": hasattr(stat, "hits_remaining"),
                            "has_wait_ms": hasattr(stat, "wait_ms"),
                            "hits_remaining_type": type(stat.hits_remaining).__name__,
                            "wait_ms_type": type(stat.wait_ms).__name__,
                        }
                    )
                return JSONResponse({"stat": None})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/fields", endpoint)]
            )

            async with make_client(app, base_url="http://test") as client:
                response = await client.get("/api/fields")
                data = response.json()

                assert data["has_key"], "Stat should have 'key' field"
                assert data["has_rate"], "Stat should have 'rate' field"
                assert data["has_hits_remaining"], (
                    "Stat should have 'hits_remaining' field"
                )
                assert data["has_wait_ms"], "Stat should have 'wait_ms' field"
                assert data["hits_remaining_type"] in ["int", "float"], (
                    "hits_remaining should be numeric"
                )
                assert data["wait_ms_type"] in ["int", "float"], (
                    "wait_ms should be numeric"
                )


@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.anyio
class TestWebSocketThrottleStat:
    async def test_stat_before_and_after_each_message(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """stat() tracks hits_remaining across a WebSocket message loop."""
        async with inmemory_backend(close_on_exit=True):
            ws_throttle = WebSocketThrottle(
                f"test-ws-stat-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                strategy=FixedWindowStrategy(),
                # Raises ConnectionThrottled instead of silently closing, so we
                # can report a "throttled" flag back to the client.
                handle_throttled=connection_throttled,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def endpoint(websocket: WebSocket) -> None:
                await websocket.accept()

                stat = await ws_throttle.stat(websocket)
                await websocket.send_json(
                    {"initial_hits_remaining": stat.hits_remaining if stat else None}
                )

                close_code = 1000
                close_reason = "Normal closure"

                try:
                    while True:
                        _ = await websocket.receive_text()
                        stat_before = await ws_throttle.stat(websocket)

                        try:
                            await ws_throttle(websocket)
                            throttled = False
                        except ConnectionThrottled as exc:
                            throttled = True
                            close_code = 1008
                            close_reason = exc.detail

                        stat_after = await ws_throttle.stat(websocket)
                        await websocket.send_json(
                            {
                                "before": stat_before.hits_remaining
                                if stat_before
                                else None,
                                "after": stat_after.hits_remaining
                                if stat_after
                                else None,
                                "throttled": throttled,
                            }
                        )

                        if throttled:
                            break
                except Exception as exc:
                    close_code = 1011
                    close_reason = str(exc)

                await asyncio.sleep(0.1)  # let the message flush before closing
                await websocket.close(code=close_code, reason=close_reason)

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

                initial_data = await ws.receive_json()
                assert initial_data["initial_hits_remaining"] == 5

                for i in range(4):
                    await ws.send_text(f"Message {i}")
                    response = await ws.receive_json()
                    assert response["before"] == 5 - i
                    assert response["after"] == 5 - (i + 1)
                    assert response["throttled"] is False

                # 5th message is the last allowed one (at limit, not yet over)
                await ws.send_text("Message 4")
                response = await ws.receive_json()
                assert response["before"] == 1
                assert response["after"] == 0
                assert response["throttled"] is False

                # 6th message goes over the limit
                await ws.send_text("Message 5")
                response = await ws.receive_json()
                assert response["before"] == 0
                assert response["throttled"] is True
