import asyncio
import os
import typing
from itertools import repeat

import anyio
import pytest
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.asyncio_client import AsyncioTestClient
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.throttles import BaseThrottle, HTTPThrottle, WebSocketThrottle
from traffik.types import UNLIMITED

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, prefix="redis-test", persistent=False)


async def _testclient_identifier(connection: HTTPConnection) -> str:
    return "testclient"


async def _unlimited_identifier(connection: HTTPConnection) -> object:
    return UNLIMITED


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.throttle
@pytest.mark.native
async def test_throttle_initialization(inmemory_backend: InMemoryBackend) -> None:
    with pytest.raises(ValueError):
        BaseThrottle("test-init-1", limit=-1)

    async def _throttle_handler(
        connection: Request,
        wait_period: int,
    ) -> None:
        # do nothing, just a placeholder for testing
        return

    # Test initialization behaviour
    async with inmemory_backend():
        throttle = BaseThrottle(
            "test-init-2",
            limit=2,
            milliseconds=10,
            seconds=50,
            minutes=2,
            hours=1,
            handle_throttled=_throttle_handler,
        )
        time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
        assert throttle.expires_after == time_in_ms
        assert throttle.backend is inmemory_backend
        assert throttle.identifier is inmemory_backend.identifier
        # Test that provided throttle handler is used
        assert throttle.handle_throttled is not inmemory_backend.handle_throttled


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.native
def test_throttle_with_app_lifespan(inmemory_backend: InMemoryBackend) -> None:
    throttle = HTTPThrottle(
        "test-throttle-app-lifespan",
        limit=2,
        milliseconds=10,
        seconds=50,
        minutes=2,
        hours=1,
        identifier=_testclient_identifier,
    )

    async def ping_endpoint(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"message": "PONG"})

    routes = [
        Route("/", ping_endpoint, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # First request should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Second request should also succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Third request should be throttled
        response = client.get("/")
        assert response.status_code == 429
        assert response.headers["Retry-After"] is not None


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.native
def test_throttle_exemption_with_identifier(inmemory_backend: InMemoryBackend) -> None:
    throttle = HTTPThrottle(
        "test-throttle-exemption",
        limit=2,
        milliseconds=10,
        seconds=50,
        minutes=2,
        hours=1,
        identifier=_unlimited_identifier,
        backend=inmemory_backend,
    )

    async def ping_endpoint(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"message": "PONG"})

    routes = [
        Route("/", ping_endpoint, methods=["GET"]),
    ]

    app = Starlette(routes=routes)

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # First request should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Second request should also succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Third request should be throttled but since the identifier is UNLIMITED,
        # it should not be throttled and should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers.get("Retry-After", None) is None


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.native
async def test_http_throttle_inmemory(inmemory_backend: InMemoryBackend) -> None:
    async with inmemory_backend():
        throttle = HTTPThrottle(
            "test-http-throttle-inmemory",
            limit=3,
            seconds=3,
            milliseconds=5,
        )
        sleep_time = 4 + (5 / 1000)

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            name = request.path_params.get("name", "unknown")
            return JSONResponse({"message": f"PONG: {name}"})

        routes = [
            Route("/{name}", ping_endpoint, methods=["GET"]),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            async with inmemory_backend(persistent=False):
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    if count == 4:
                        await anyio.sleep(sleep_time)
                    response = await client.get(f"{base_url}/{name}")
                    assert response.status_code == 200
                    assert response.json() == {"message": f"PONG: {name}"}
            # Backend reset on context exit to ensure the throttle is cleared

            async with inmemory_backend(persistent=False):
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    response = await client.get(f"/{name}")
                    if count > 3:
                        assert response.status_code == 429
                        assert response.headers["Retry-After"] is not None
                    else:
                        assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
async def test_http_throttle_redis(redis_backend: RedisBackend) -> None:
    async with redis_backend():
        throttle = HTTPThrottle(
            "test-http-throttle-redis",
            limit=3,
            seconds=3,
            milliseconds=5,
        )
        sleep_time = 4 + (5 / 1000)

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            name = request.path_params.get("name", "unknown")
            return JSONResponse({"message": f"PONG: {name}"})

        routes = [
            Route("/{name}", ping_endpoint, methods=["GET"]),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            async with redis_backend(persistent=False):
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    if count == 4:
                        await anyio.sleep(sleep_time)
                    response = await client.get(f"{base_url}/{name}")
                    assert response.status_code == 200
                    assert response.json() == {"message": f"PONG: {name}"}
            # Backend reset on context exit to ensure the throttle is cleared

            async with redis_backend(persistent=False):
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    response = await client.get(f"/{name}")
                    if count > 3:
                        assert response.status_code == 429
                        assert response.headers["Retry-After"] is not None
                    else:
                        assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.concurrent
@pytest.mark.native
async def test_http_throttle_inmemory_concurrent(
    inmemory_backend: InMemoryBackend,
) -> None:
    async with inmemory_backend():
        throttle = HTTPThrottle(
            "test-http-throttle-inmemory-concurrent",
            limit=3,
            seconds=5,
            milliseconds=5,
        )

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            name = request.path_params.get("name", "unknown")
            return JSONResponse({"message": f"PONG: {name}"})

        routes = [
            Route("/{name}", ping_endpoint, methods=["GET"]),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:

            async def make_request(name) -> Response:
                return await client.get(f"{base_url}/{name}")

            responses = await asyncio.gather(
                *(make_request(name) for name in repeat("test-client", 5))
            )
            status_codes = [r.status_code for r in responses]
            assert status_codes.count(200) == 3
            assert status_codes.count(429) == 2


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.concurrent
@pytest.mark.redis
async def test_http_throttle_redis_concurrent(redis_backend: RedisBackend) -> None:
    async with redis_backend():
        throttle = HTTPThrottle(
            "test-http-throttle-redis-concurrent",
            limit=3,
            seconds=5,
            milliseconds=5,
        )

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            name = request.path_params.get("name", "unknown")
            return JSONResponse({"message": f"PONG: {name}"})

        routes = [
            Route("/{name}", ping_endpoint, methods=["GET"]),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:

            async def make_request(name) -> Response:
                return await client.get(f"{base_url}/{name}")

            responses = await asyncio.gather(
                *(make_request(name) for name in repeat("test-client", 5))
            )
            status_codes = [r.status_code for r in responses]
            assert status_codes.count(200) == 3
            assert status_codes.count(429) == 2


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.native
async def test_websocket_throttle_inmemory(inmemory_backend: InMemoryBackend) -> None:
    async with inmemory_backend():
        throttle = WebSocketThrottle(
            "test-websocket-throttle-inmemory",
            limit=3,
            seconds=5,
            milliseconds=5,
            identifier=_testclient_identifier,
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            print("ACCEPTED WEBSOCKET CONNECTION")
            close_code = 1000  # Normal closure
            close_reason = "Normal closure"
            while True:
                try:
                    data = await websocket.receive_json()
                    await throttle(websocket)
                    await websocket.send_json(
                        {
                            "status": "success",
                            "status_code": 200,
                            "headers": {},
                            "detail": "Request successful",
                            "data": data,
                        }
                    )
                except HTTPException as exc:
                    print("HTTP EXCEPTION:", exc)
                    await websocket.send_json(
                        {
                            "status": "error",
                            "status_code": exc.status_code,
                            "detail": exc.detail,
                            "headers": exc.headers,
                            "data": None,
                        }
                    )
                    close_reason = exc.detail
                    break
                except Exception as exc:
                    print("WEBSOCKET ERROR:", exc)
                    await websocket.send_json(
                        {
                            "status": "error",
                            "status_code": 500,
                            "detail": "Operation failed",
                            "headers": {},
                            "data": None,
                        }
                    )
                    close_code = 1011  # Internal error
                    close_reason = "Internal error"
                    break

            # Allow time for the message put in the queue to be processed
            # and received by the client before closing the websocket
            await asyncio.sleep(1)
            await websocket.close(code=close_code, reason=close_reason)

        routes = [
            WebSocketRoute("/ws/", ws_endpoint),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with AsyncioTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client, client.websocket_connect(url="/ws/") as ws:
            # Reset the backend before starting the test
            # as connecting to the websocket already counts as a request
            # and we want to start fresh.
            await inmemory_backend.reset()

            async with inmemory_backend(persistent=False):

                async def make_ws_request() -> typing.Tuple[str, int]:
                    try:
                        await ws.send_json({"message": "ping"})
                        response = await ws.receive_json()
                        return response["status"], response["status_code"]
                    except WebSocketDisconnect as exc:
                        print("WEBSOCKET DISCONNECT:", exc)
                        return "disconnected", 1000

                for count in range(1, 6):
                    result = await make_ws_request()
                    assert result[0] == "success"
                    assert result[1] == 200
                    if count == 3:
                        sleep_time = (
                            5 + (5 / 1000) + 2
                        )  # For the last request, we wait a bit longer
                        await asyncio.sleep(sleep_time)
            # Backend reset on context exit to ensure the throttle is cleared

            await asyncio.sleep(0.01)
            async with inmemory_backend(persistent=False):
                for count in range(1, 4):
                    result = await make_ws_request()
                    if count > 3:
                        # After the third request, the throttle should kick in
                        # and subsequent requests should fail
                        assert result[0] == "error"
                        assert result[1] == 429
                    else:
                        assert result[0] == "success"
                        assert result[1] == 200


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.redis
async def test_websocket_throttle_redis(redis_backend: RedisBackend) -> None:
    async with redis_backend():
        throttle = WebSocketThrottle(
            "test-websocket-throttle-redis",
            limit=3,
            seconds=5,
            milliseconds=5,
            identifier=_testclient_identifier,
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            print("ACCEPTED WEBSOCKET CONNECTION")
            close_code = 1000  # Normal closure
            close_reason = "Normal closure"
            while True:
                try:
                    data = await websocket.receive_json()
                    await throttle(websocket)
                    await websocket.send_json(
                        {
                            "status": "success",
                            "status_code": 200,
                            "headers": {},
                            "detail": "Request successful",
                            "data": data,
                        }
                    )
                except HTTPException as exc:
                    print("HTTP EXCEPTION:", exc)
                    await websocket.send_json(
                        {
                            "status": "error",
                            "status_code": exc.status_code,
                            "detail": exc.detail,
                            "headers": exc.headers,
                            "data": None,
                        }
                    )
                    close_reason = exc.detail
                    break
                except Exception as exc:
                    print("WEBSOCKET ERROR:", exc)
                    await websocket.send_json(
                        {
                            "status": "error",
                            "status_code": 500,
                            "detail": "Operation failed",
                            "headers": {},
                            "data": None,
                        }
                    )
                    close_code = 1011  # Internal error
                    close_reason = "Internal error"
                    break

            # Allow time for the message put in the queue to be processed
            # and received by the client before closing the websocket
            await asyncio.sleep(0.1)
            await websocket.close(code=close_code, reason=close_reason)

        routes = [
            WebSocketRoute("/ws/", ws_endpoint),
        ]

        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with AsyncioTestClient(
            app=app,
            base_url=base_url,
            event_loop=running_loop,
        ) as client, client.websocket_connect(url="/ws/") as ws:
            # Reset the backend before starting the test
            # as connecting to the websocket already counts as a request
            # and we want to start fresh.
            await redis_backend.reset()

            async with redis_backend(persistent=False):

                async def make_ws_request() -> typing.Tuple[str, int]:
                    try:
                        await ws.send_json({"message": "ping"})
                        response = await ws.receive_json()
                        return response["status"], response["status_code"]
                    except WebSocketDisconnect as exc:
                        print("WEBSOCKET DISCONNECT:", exc)
                        return "disconnected", 1000

                for count in range(1, 6):
                    result = await make_ws_request()
                    assert result[0] == "success"
                    assert result[1] == 200
                    if count == 3:
                        sleep_time = (
                            5 + (5 / 1000) + 1
                        )  # For the last request, we wait a bit longer
                        await asyncio.sleep(sleep_time)
            # Backend reset on context exit to ensure the throttle is cleared

            async with redis_backend(persistent=False):
                for count in range(1, 4):
                    result = await make_ws_request()
                    if count > 3:
                        # After the third request, the throttle should kick in
                        # and subsequent requests should fail
                        assert result[0] == "error"
                        assert result[1] == 429
                    else:
                        assert result[0] == "success"
                        assert result[1] == 200

