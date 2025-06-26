import typing
import asyncio
import pytest
from itertools import repeat
from httpx import AsyncClient, ASGITransport, Response
from starlette.websockets import WebSocket
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from fastapi_throttle.backends.inmemory import InMemoryBackend
from fastapi_throttle.backends.redis import RedisBackend
from fastapi_throttle.exceptions import ConfigurationError
from fastapi_throttle.throttles import BaseThrottle, HTTPThrottle, WebSocketThrottle


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
        connection="redis://localhost:6379/0",
        prefix="redis-test",
        persistent=False,
    )


async def test_throttle_initialization(inmemory_backend: InMemoryBackend) -> None:
    # Test that an error is raised when not backend is passed or detected
    with pytest.raises(ConfigurationError):
        BaseThrottle()

    # Test initialization behaviour
    async with inmemory_backend:
        throttle = BaseThrottle(
            limit=2,
            milliseconds=10,
            seconds=50,
            minutes=2,
            hours=1,
            handle_throttled=lambda x: x,
        )
        time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
        assert throttle.expires_after == time_in_ms
        assert throttle.backend is inmemory_backend
        assert throttle.identifier is inmemory_backend.identifier
        # Test that provided throttle handler is used
        assert throttle.handle_throttled is not inmemory_backend.handle_throttled

    with pytest.raises(ValueError):
        BaseThrottle(limit=-1)


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
        async def ping(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            for i in range(1, 5):
                if i == 4:
                    await asyncio.sleep(sleep_time)
                response = await client.get(f"{base_url}/{i}")
                assert response.status_code == 200

            await inmemory_backend.reset()
            for count, name in enumerate(repeat("test-client", 5), start=1):
                response = await client.get(f"/{name}")
                if count > 3:
                    assert response.status_code == 429
                else:
                    assert response.status_code == 200


@pytest.mark.anyio
async def test_http_throttle_redis(
    redis_backend: RedisBackend, app: FastAPI
) -> None:
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
        async def ping(name: str) -> typing.Dict[str, str]:
            return {"message": f"PONG: {name}"}

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=base_url,
        ) as client:
            for i in range(1, 5):
                if i == 4:
                    await asyncio.sleep(sleep_time)
                response = await client.get(f"{base_url}/{i}")
                assert response.status_code == 200

            await redis_backend.reset()
            for count, name in enumerate(repeat("test-client", 5), start=1):
                response = await client.get(f"/{name}")
                if count > 3:
                    assert response.status_code == 429
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
        async def ping(name: str) -> typing.Dict[str, str]:
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
        async def ping(name: str) -> typing.Dict[str, str]:
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
async def test_websocket_throttle_inmemory_concurrent(
    inmemory_backend: InMemoryBackend, app: FastAPI
) -> None:
    async with inmemory_backend:
        throttle = WebSocketThrottle(
            limit=3,
            seconds=3,
            milliseconds=5,
        )

        @app.websocket("/ws/{name}")
        async def ws_endpoint(websocket: WebSocket, name: str) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": f"PONG: {name}"})
            await websocket.close()

        base_url = "http://0.0.0.0"
        client = TestClient(
            app=app,
            base_url=base_url,
            client=("127.0.0.1", 123),
        )

        async def make_ws_request(name):
            ws_url = f"/ws/{name}"
            try:
                with client.websocket_connect(ws_url) as ws:
                    data = ws.receive_json()
                    return 200, data
            except Exception as exc:
                print(exc)
                return 429, str(exc)

        results = await asyncio.gather(
            *(make_ws_request(name) for name in repeat("test-client", 5))
        )
        status_codes = [r[0] for r in results]
        assert status_codes.count(200) == 3
        assert status_codes.count(429) == 2
        client.close()


@pytest.mark.asyncio
async def test_websocket_throttle_redis_concurrent(
    redis_backend: RedisBackend, app: FastAPI
) -> None:
    async with redis_backend:
        throttle = WebSocketThrottle(
            limit=3,
            seconds=3,
            milliseconds=5,
        )

        @app.websocket("/ws/{name}")
        async def ws_endpoint(websocket: WebSocket, name: str) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": f"PONG: {name}"})
            await websocket.close()

        base_url = "http://0.0.0.0"
        client = TestClient(
            app=app,
            base_url=base_url,
            client=("127.0.0.1", 123),
        )

        async def make_ws_request(name):
            ws_url = f"/ws/{name}"
            try:
                with client.websocket_connect(ws_url) as ws:
                    data = ws.receive_json()
                    return 200, data
            except Exception as exc:
                print(exc)
                return 429, str(exc)

        results = await asyncio.gather(
            *(make_ws_request(name) for name in repeat("test-client", 5))
        )
        status_codes = [r[0] for r in results]
        assert status_codes.count(200) == 3
        assert status_codes.count(429) == 2
        client.close()
