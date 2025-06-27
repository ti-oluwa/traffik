import asyncio
import os
import typing
from itertools import repeat

import anyio
import pytest
from fastapi import Depends, FastAPI, WebSocketDisconnect
from httpx import ASGITransport, AsyncClient, Response
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from tests.asyncio_client import AsyncioTestClient
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.throttles import BaseThrottle, HTTPThrottle, WebSocketThrottle

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
async def app() -> FastAPI:
    app = FastAPI()
    return app


@pytest.fixture(scope="function")
async def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    return RedisBackend(
        connection=REDIS_URL,
        prefix="redis-test",
        persistent=False,
    )


async def _testclient_identifier(connection: WebSocket) -> str:
    return "testclient"


@pytest.mark.asyncio
async def test_throttle_initialization(inmemory_backend: InMemoryBackend) -> None:
    with pytest.raises(ValueError):
        BaseThrottle(limit=-1)

    async def _throttle_handler(
        connection: HTTPConnection,
        wait_period: int,
    ) -> None:
        # do nothing, just a placeholder for testing
        return

    # Test initialization behaviour
    async with inmemory_backend:
        throttle = BaseThrottle(
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


@pytest.mark.anyio
async def test_http_throttle_inmemory(
    inmemory_backend: InMemoryBackend, app: FastAPI
) -> None:
    async with inmemory_backend:
        throttle = HTTPThrottle(
            limit=3,
            seconds=3,
            milliseconds=5,
        )
        sleep_time = 3 + (5 / 1000)

        @app.get(
            "/{name}",
            dependencies=[Depends(throttle)],
            status_code=200,
        )
        async def ping_endpoint(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            for count, name in enumerate(repeat("test-client", 5), start=1):
                if count == 4:
                    await anyio.sleep(sleep_time)
                response = await client.get(f"{base_url}/{name}")
                assert response.status_code == 200
                assert response.json() == {"message": f"PONG: {name}"}

            await inmemory_backend.reset()
            for count, name in enumerate(repeat("test-client", 5), start=1):
                response = await client.get(f"/{name}")
                if count > 3:
                    assert response.status_code == 429
                    assert response.headers["Retry-After"] is not None
                else:
                    assert response.status_code == 200


@pytest.mark.anyio
async def test_http_throttle_redis(redis_backend: RedisBackend, app: FastAPI) -> None:
    async with redis_backend:
        throttle = HTTPThrottle(
            limit=3,
            seconds=3,
            milliseconds=5,
        )
        sleep_time = 3 + (5 / 1000)

        @app.get(
            "/{name}",
            dependencies=[Depends(throttle)],
            status_code=200,
        )
        async def ping_endpoint(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            for count, name in enumerate(repeat("test-client", 5), start=1):
                if count == 4:
                    await anyio.sleep(sleep_time)
                response = await client.get(f"{base_url}/{name}")
                assert response.status_code == 200
                assert response.json() == {"message": f"PONG: {name}"}

            await redis_backend.reset()
            for count, name in enumerate(repeat("test-client", 5), start=1):
                response = await client.get(f"/{name}")
                if count > 3:
                    assert response.status_code == 429
                    assert response.headers["Retry-After"] is not None
                else:
                    assert response.status_code == 200


@pytest.mark.anyio
async def test_http_throttle_inmemory_concurrent(
    inmemory_backend: InMemoryBackend, app: FastAPI
) -> None:
    async with inmemory_backend:
        throttle = HTTPThrottle(
            limit=3,
            seconds=5,
            milliseconds=5,
        )

        @app.get(
            "/concurrent/{name}",
            dependencies=[Depends(throttle)],
            status_code=200,
        )
        async def ping_endpoint(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:

            async def make_request(name):
                return await client.get(f"{base_url}/concurrent/{name}")

            responses = await asyncio.gather(
                *(make_request(name) for name in repeat("test-client", 5))
            )
            status_codes = [r.status_code for r in responses]
            assert status_codes.count(200) == 3
            assert status_codes.count(429) == 2


@pytest.mark.anyio
async def test_http_throttle_redis_concurrent(
    redis_backend: RedisBackend, app: FastAPI
) -> None:
    async with redis_backend:
        throttle = HTTPThrottle(
            limit=3,
            seconds=5,
            milliseconds=5,
        )

        @app.get(
            "/concurrent/{name}",
            dependencies=[Depends(throttle)],
            status_code=200,
        )
        async def ping_endpoint(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "ws://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:

            async def make_request(name) -> Response:
                return await client.get(f"{base_url}/concurrent/{name}")

            responses = await asyncio.gather(
                *(make_request(name) for name in repeat("test-client", 5))
            )
            status_codes = [r.status_code for r in responses]
            assert status_codes.count(200) == 3
            assert status_codes.count(429) == 2


@pytest.mark.anyio
async def test_websocket_throttle_inmemory(
    inmemory_backend: InMemoryBackend, app: FastAPI
) -> None:
    async with inmemory_backend:
        throttle = WebSocketThrottle(
            limit=3,
            seconds=5,
            milliseconds=5,
            identifier=_testclient_identifier,
        )

        @app.websocket("/ws/")
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
            await websocket.close(code=close_code, reason=close_reason)

        base_url = "http://0.0.0.0"
        async with AsyncioTestClient(
            app=app,
            base_url=base_url,
            event_loop=asyncio.get_running_loop(),
        ) as client, client.websocket_connect(url="/ws/") as ws:
            # Reset the backend before starting the test
            # as connecting to the websocket already counts as a request
            # and we want to start fresh.
            await inmemory_backend.reset()

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
                    await anyio.sleep(6 + (5 / 1000))

            await inmemory_backend.reset()
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


@pytest.mark.anyio
async def test_websocket_throttle_redis(
    redis_backend: RedisBackend, app: FastAPI
) -> None:
    async with redis_backend:
        throttle = WebSocketThrottle(
            limit=3,
            seconds=5,
            milliseconds=5,
            identifier=_testclient_identifier,
        )

        @app.websocket("/ws/")
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
            await websocket.close(code=close_code, reason=close_reason)

        base_url = "http://0.0.0.0"
        async with AsyncioTestClient(
            app=app,
            base_url=base_url,
            event_loop=asyncio.get_running_loop(),
        ) as client, client.websocket_connect(url="/ws/") as ws:
            # Reset the backend before starting the test
            # as connecting to the websocket already counts as a request
            # and we want to start fresh.
            await redis_backend.reset()

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
                    await anyio.sleep(5 + (5 / 1000))

            await redis_backend.reset()
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
