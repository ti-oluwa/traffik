"""Tests for stat functionality in HTTP and WebSocket throttles."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from tests.asynctestclient import AsyncTestClient
from tests.utils import default_client_identifier
from traffik import connection_throttled
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_stat_basic(inmemory_backend: InMemoryBackend) -> None:
    """Test HTTPThrottle.stat() method returns statistics."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat-basic",
            rate="10/s",
            identifier=default_client_identifier,
            strategy=FixedWindowStrategy(),
        )

        async def data_endpoint(request: Request) -> JSONResponse:
            # Get stat before throttling
            stat_before = await throttle.stat(request)
            await throttle(request)
            # Get stat after throttling
            stat_after = await throttle.stat(request)
            return JSONResponse(
                {
                    "before": {
                        "hits_remaining": stat_before.hits_remaining
                        if stat_before
                        else None,
                        "wait_time": stat_before.wait_time if stat_before else None,
                    },
                    "after": {
                        "hits_remaining": stat_after.hits_remaining
                        if stat_after
                        else None,
                        "wait_time": stat_after.wait_time if stat_after else None,
                    },
                }
            )

        routes = [Route("/api/data", data_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # First request
            response = await client.get("/api/data")
            assert response.status_code == 200
            data = response.json()

            # Before first request: 10 hits remaining
            assert data["before"]["hits_remaining"] == 10
            assert data["before"]["wait_time"] == 0.0

            # After first request: 9 hits remaining
            assert data["after"]["hits_remaining"] == 9
            assert data["after"]["wait_time"] == 0.0


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_stat_with_cost(inmemory_backend: InMemoryBackend) -> None:
    """Test HTTPThrottle.stat() reflects cost properly."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat-cost",
            rate="20/s",
            identifier=default_client_identifier,
            cost=5,  # Each request costs 5
            strategy=FixedWindowStrategy(),
        )

        async def expensive_endpoint(request: Request) -> JSONResponse:
            stat_before = await throttle.stat(request)
            await throttle(request)
            stat_after = await throttle.stat(request)
            return JSONResponse(
                {
                    "before": stat_before.hits_remaining if stat_before else None,
                    "after": stat_after.hits_remaining if stat_after else None,
                }
            )

        routes = [Route("/api/expensive", expensive_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # First request
            response = await client.get("/api/expensive")
            data = response.json()

            # Cost=5, so should go from 20 to 15
            assert data["before"] == 20
            assert data["after"] == 15

            # Second request
            response = await client.get("/api/expensive")
            data = response.json()

            # Should go from 15 to 10
            assert data["before"] == 15
            assert data["after"] == 10


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_stat_at_limit(inmemory_backend: InMemoryBackend) -> None:
    """Test HTTPThrottle.stat() when at rate limit."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat-limit",
            rate="3/s",
            identifier=default_client_identifier,
            strategy=FixedWindowStrategy(),
        )

        async def limited_endpoint(request: Request) -> JSONResponse:
            try:
                await throttle(request)
                throttled = False
            except Exception:
                throttled = True
            stat = await throttle.stat(request)
            return JSONResponse(
                {
                    "hits_remaining": stat.hits_remaining if stat else None,
                    "wait_time": stat.wait_time if stat else None,
                    "throttled": throttled,
                }
            )

        routes = [Route("/api/limited", limited_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Use up all 3 requests
            for i in range(3):
                response = await client.get("/api/limited")
                assert response.status_code == 200
                data = response.json()
                assert data["hits_remaining"] == 3 - (i + 1)
                assert data["throttled"] is False

            # 4th request should show 0 hits remaining and wait time
            response = await client.get("/api/limited")
            assert response.status_code == 200
            data = response.json()
            assert data["hits_remaining"] == 0
            assert data["wait_time"] > 0.0
            assert data["throttled"] is True


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_stat_different_keys(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test HTTPThrottle.stat() for different clients."""
    async with inmemory_backend(close_on_exit=True):

        async def custom_identifier(connection: Request) -> str:
            # Use query parameter as identifier for testing
            return connection.query_params.get("user", "anonymous")

        throttle = HTTPThrottle(
            "test-stat-keys",
            rate="5/s",
            identifier=custom_identifier,
            strategy=FixedWindowStrategy(),
        )

        async def resource_endpoint(request: Request) -> JSONResponse:
            stat = await throttle.stat(request)
            await throttle(request)
            return JSONResponse(
                {
                    "user": request.query_params.get("user"),
                    "hits_remaining": stat.hits_remaining if stat else None,
                }
            )

        routes = [Route("/api/resource", resource_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # User 1 makes 2 requests
            response = await client.get("/api/resource?user=user1")
            assert response.json()["hits_remaining"] == 5

            response = await client.get("/api/resource?user=user1")
            assert response.json()["hits_remaining"] == 4

            # User 2 should have independent stat
            response = await client.get("/api/resource?user=user2")
            assert response.json()["hits_remaining"] == 5

            # User 1 should still be at 3
            response = await client.get("/api/resource?user=user1")
            assert response.json()["hits_remaining"] == 3


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_http_throttle_stat_unlimited_rate(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test HTTPThrottle.stat() with unlimited rate."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat-unlimited",
            rate=Rate(limit=0, seconds=0),  # Unlimited
            identifier=default_client_identifier,
            strategy=FixedWindowStrategy(),
        )

        async def unlimited_endpoint(request: Request) -> JSONResponse:
            stat = await throttle.stat(request)
            await throttle(request)
            return JSONResponse(
                {
                    "is_infinite": (
                        str(stat.hits_remaining) == "inf" if stat else False
                    ),
                }
            )

        routes = [Route("/api/unlimited", unlimited_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Make many requests
            for _ in range(100):
                response = await client.get("/api/unlimited")
                data = response.json()
                assert data["is_infinite"], "Should always show infinite hits"


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_websocket_throttle_stat(inmemory_backend: InMemoryBackend) -> None:
    """Test WebSocketThrottle.stat() method."""
    async with inmemory_backend(close_on_exit=True):
        ws_throttle = WebSocketThrottle(
            "test-ws-stat",
            rate="5/s",
            identifier=default_client_identifier,
            strategy=FixedWindowStrategy(),
            # Use this handler so an exception is raised on throttle
            handle_throttled=connection_throttled,
        )

        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()

            # Get initial stat
            stat = await ws_throttle.stat(websocket)
            await websocket.send_json(
                {"initial_hits_remaining": stat.hits_remaining if stat else None}
            )

            close_code = 1000  # Normal closure
            close_reason = "Normal closure"

            try:
                while True:
                    _ = await websocket.receive_text()

                    # Get stat before throttle
                    stat_before = await ws_throttle.stat(websocket)

                    # Apply throttle
                    try:
                        await ws_throttle(websocket)
                        throttled = False
                    except HTTPException as exc:
                        throttled = True
                        close_code = 1008  # Policy Violation
                        close_reason = exc.detail

                    # Get stat after throttle
                    stat_after = await ws_throttle.stat(websocket)

                    await websocket.send_json(
                        {
                            "before": stat_before.hits_remaining
                            if stat_before
                            else None,
                            "after": stat_after.hits_remaining if stat_after else None,
                            "throttled": throttled,
                        }
                    )

                    if throttled:
                        break
            except Exception as exc:
                close_code = 1011  # Internal Error
                close_reason = str(exc)

            await asyncio.sleep(0.1)  # Ensure message is sent
            await websocket.close(code=close_code, reason=close_reason)

        routes = [WebSocketRoute("/ws", websocket_endpoint)]
        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with (
            AsyncTestClient(
                app=app,
                base_url=base_url,
                event_loop=running_loop,
            ) as client,
            client.websocket_connect(url="/ws") as ws,
        ):
            # Reset backend after connection (connection counts as a request)
            await inmemory_backend.reset()
            await inmemory_backend.initialize()

            # Get initial stat
            initial_data = await ws.receive_json()
            assert initial_data["initial_hits_remaining"] == 5

            # Make 4 requests (should all succeed)
            for i in range(4):
                await ws.send_text(f"Message {i}")
                response = await ws.receive_json()
                assert response["before"] == 5 - i
                assert response["after"] == 5 - (i + 1)
                assert response["throttled"] is False

            # 5th request should still succeed (at limit)
            await ws.send_text("Message 4")
            response = await ws.receive_json()
            assert response["before"] == 1
            assert response["after"] == 0
            assert response["throttled"] is False

            # 6th request should be throttled
            await ws.send_text("Message 5")
            response = await ws.receive_json()
            assert response["before"] == 0
            assert response["throttled"] is True


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttle_stat_without_strategy_support(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that throttles handle strategies without get_stat method."""
    async with inmemory_backend(close_on_exit=True):
        # Create a simple strategy without get_stat
        async def simple_strategy(key, rate, backend, cost=1):
            return 0.0  # Always allow

        throttle = HTTPThrottle(
            "test-no-stat",
            rate="10/s",
            identifier=default_client_identifier,
            strategy=simple_strategy,
        )

        async def no_stat_endpoint(request: Request) -> JSONResponse:
            stat = await throttle.stat(request)
            return JSONResponse({"stat": stat})

        routes = [Route("/api/no-stat", no_stat_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            response = await client.get("/api/no-stat")
            data = response.json()
            # Should return None when strategy doesn't support stats
            assert data["stat"] is None


@pytest.mark.asyncio
@pytest.mark.throttle
async def test_throttle_stat_fields(inmemory_backend: InMemoryBackend) -> None:
    """Test that HTTPThrottle.stat() returns all required fields."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            "test-stat-fields",
            rate="10/s",
            identifier=default_client_identifier,
            strategy=FixedWindowStrategy(),
        )

        async def fields_endpoint(request: Request) -> JSONResponse:
            stat = await throttle.stat(request)

            if stat:
                return JSONResponse(
                    {
                        "has_key": hasattr(stat, "key"),
                        "has_rate": hasattr(stat, "rate"),
                        "has_hits_remaining": hasattr(stat, "hits_remaining"),
                        "has_wait_time": hasattr(stat, "wait_time"),
                        "hits_remaining_type": type(stat.hits_remaining).__name__,
                        "wait_time_type": type(stat.wait_time).__name__,
                    }
                )
            return JSONResponse({"stat": None})

        routes = [Route("/api/fields", fields_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://test"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            response = await client.get("/api/fields")
            data = response.json()

            assert data["has_key"], "Stat should have 'key' field"
            assert data["has_rate"], "Stat should have 'rate' field"
            assert data["has_hits_remaining"], "Stat should have 'hits_remaining' field"
            assert data["has_wait_time"], "Stat should have 'wait_time' field"
            assert data["hits_remaining_type"] in ["int", "float"], (
                "hits_remaining should be numeric"
            )
            assert data["wait_time_type"] in ["int", "float"], (
                "wait_time should be numeric"
            )
