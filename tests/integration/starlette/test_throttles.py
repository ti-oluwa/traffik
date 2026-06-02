import asyncio
import typing
from itertools import repeat

import anyio
import pytest
from httpx2 import ASGITransport, AsyncClient, Response

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from tests.asynctestclient import AsyncTestClient
from tests.conftest import BackendGen
from tests.utils import (
    default_client_identifier,
    requires_throttle_type,
    unlimited_identifier,
)
from traffik import strategies
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import DEFAULT_HEADERS_ALWAYS
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.strategies.sliding_window import SlidingWindowLogStrategy
from traffik.throttles import HTTPThrottle, Throttle, WebSocketThrottle


@pytest.mark.anyio
@pytest.mark.throttle
@requires_throttle_type
class TestThrottleBasic:
    async def test_throttle_initialization(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        with pytest.raises(ValueError):
            throttle_type(
                "test-init-1",
                rate=Rate(limit=-1),
                registry=ThrottleRegistry(),
            )

        async def _throttle_handler(
            connection: HTTPConnection, wait_ms: float, *args, **kwargs
        ) -> None:
            # do nothing, just a placeholder for testing
            return

        # Test initialization behaviour
        throttle = throttle_type(
            "test-init-2-sl",
            rate=Rate(limit=2, milliseconds=10, seconds=50, minutes=2, hours=1),
            handle_throttled=_throttle_handler,
            registry=ThrottleRegistry(),
        )
        time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
        assert throttle.rate.expire == time_in_ms  # type: ignore[attr-defined]
        assert throttle.backend is backend
        assert throttle.identifier is backend.identifier
        # Test that provided throttle handler is used
        assert throttle.handle_throttled is not backend.handle_throttled

    async def test_throttle_is_disabled_default_false(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-default",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        assert throttle.is_disabled is False

    async def test_throttle_disable_sets_flag(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-flag",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()
        assert throttle.is_disabled is True

    async def test_throttle_enable_clears_flag(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-enable",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()
        await throttle.enable()
        assert throttle.is_disabled is False

    async def test_update_rate_string(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        """update_rate() should parse a rate string and update _uses_rate_func."""
        throttle = throttle_type(
            uid="upd-rate-str",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.update_rate("100/s")
        assert throttle.rate == Rate.parse("100/s")
        assert throttle._uses_rate_func is False

    async def test_update_rate_rate_object(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-rate-obj",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        new_rate = Rate.parse("200/h")
        await throttle.update_rate(new_rate)
        assert throttle.rate is new_rate
        assert throttle._uses_rate_func is False

    async def test_update_rate_callable(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-rate-fn",
            rate="5/min",
            backend=backend,
            identifier=default_client_identifier,
        )

        async def dynamic_rate(conn, ctx):
            return Rate.parse("50/min")

        await throttle.update_rate(dynamic_rate)
        assert throttle._uses_rate_func is True

    async def test_update_cost_static(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-cost",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.update_cost(3)
        assert throttle.cost == 3
        assert throttle._uses_cost_func is False

    async def test_update_cost_callable(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-cost-fn",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )

        async def dynamic_cost(conn, ctx):
            return 5

        await throttle.update_cost(dynamic_cost)
        assert throttle._uses_cost_func is True

    async def test_update_min_wait_period(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-mwp",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.update_min_wait_period(500)
        assert throttle.min_wait_period == 500
        await throttle.update_min_wait_period(None)
        assert throttle.min_wait_period is None

    async def test_update_identifier(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-ident",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )

        async def new_identifier(conn):
            return "custom-id"

        await throttle.update_identifier(new_identifier)
        assert throttle.identifier is new_identifier

    async def test_update_headers_none_clears(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-hdrs",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
            headers=DEFAULT_HEADERS_ALWAYS,
        )
        await throttle.update_headers(None)
        assert len(throttle._headers) == 0

    async def test_update_strategy(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-strategy",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        new_strategy = SlidingWindowLogStrategy()
        await throttle.update_strategy(new_strategy)
        assert throttle.strategy is new_strategy

    async def test_update_backend(
        self,
        backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-backend",
            rate="10/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        new_backend = InMemoryBackend(persistent=False)
        await throttle.update_backend(new_backend)
        assert throttle.backend is new_backend


@pytest.mark.anyio
@pytest.mark.throttle
class TestHTTPThrottle:
    async def test_http_throttle(self, backends: BackendGen) -> None:
        for backend in backends(persistent=False, namespace="http_throttle_test"):
            async with backend(close_on_exit=True):
                throttle = HTTPThrottle(
                    "test-http-throttle-sl",
                    rate="3/3500ms",
                    registry=ThrottleRegistry(),
                )
                sleep_time = 3.5

                async def ping_endpoint(request: Request) -> JSONResponse:
                    await throttle(request)
                    name = request.path_params.get("name", "unknown")
                    return JSONResponse({"message": f"PONG: {name}"})

                routes = [Route("/{name}", ping_endpoint, methods=["GET"])]
                app = Starlette(routes=routes)

                base_url = "http://0.0.0.0"
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url=base_url,
                ) as client:
                    for count, name in enumerate(
                        repeat("test-client", times=5), start=1
                    ):
                        if count == 4:
                            await anyio.sleep(sleep_time)
                        response = await client.get(f"/{name}")
                        assert response.status_code == 200
                        assert response.json() == {"message": f"PONG: {name}"}

                    await backend.reset()
                    # await backend.initialize()
                    for count, name in enumerate(
                        repeat("test-client", times=5), start=1
                    ):
                        response = await client.get(f"/{name}")
                        if count > 3:
                            assert response.status_code == 429
                            assert response.headers["Retry-After"] is not None
                        else:
                            assert response.status_code == 200

    async def test_http_throttle_with_lifespan(self, backend: InMemoryBackend) -> None:
        throttle = HTTPThrottle(
            "test-throttle-app-lifespan-sl",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "PONG"})

        routes = [Route("/", ping_endpoint, methods=["GET"])]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # First request should succeed
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "PONG"}

            # Second request should also succeed
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "PONG"}

            # Third request should be throttled
            response = await client.get("/")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None

    async def test_http_throttle_exemption_with_unlimited_identifier(
        self, backend: InMemoryBackend
    ) -> None:
        throttle = HTTPThrottle(
            "test-throttle-exemption-sl",
            rate="2/s",
            identifier=unlimited_identifier,
            backend=backend,
            registry=ThrottleRegistry(),
        )

        async def ping_endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "PONG"})

        routes = [Route("/", ping_endpoint, methods=["GET"])]
        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # First request should succeed
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "PONG"}

            # Second request should also succeed
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "PONG"}

            # Third request should be throttled but since the identifier is EXEMPTED,
            # it should not be throttled and should succeed
            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers.get("Retry-After", None) is None

    async def test_disabled_http_throttle_skips_hit(
        self, backend: InMemoryBackend
    ) -> None:
        """A disabled throttle should pass all requests through, even past the rate limit."""
        throttle = HTTPThrottle(
            uid="dis-skip-hit",
            rate="1/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()

        transport = ASGITransport(
            app=Starlette(
                routes=[Route("/test", lambda req: JSONResponse({"ok": True}))]
            )
        )
        async with AsyncClient(transport=transport, base_url="http://test"):
            req = Request(
                scope={
                    "type": "http",
                    "method": "GET",
                    "path": "/test",
                    "query_string": b"",
                    "headers": [],
                }
            )
            # Fire 5 requests — all should pass because the throttle is disabled
            for _ in range(5):
                result = await throttle.hit(req)
                assert result is req

    @pytest.mark.concurrent
    async def test_http_throttle_concurrent(self, backends: BackendGen) -> None:
        for backend in backends(persistent=False, namespace="http_throttle_concurrent"):
            async with backend(close_on_exit=True):
                throttle = HTTPThrottle(
                    "http-throttle-concurrent-sl",
                    rate="3/1050ms",
                    strategy=strategies.TokenBucketStrategy(),
                    registry=ThrottleRegistry(),
                )

                async def ping_endpoint(request: Request) -> JSONResponse:
                    nonlocal throttle
                    await throttle(request)
                    name = request.path_params.get("name", "unknown")
                    return JSONResponse({"message": f"PONG: {name}"})

                routes = [Route("/{name}", ping_endpoint, methods=["GET"])]
                app = Starlette(routes=routes)

                base_url = "http://0.0.0.0"
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url=base_url,
                ) as client:

                    async def make_request(name: str) -> Response:
                        return await client.get(f"{base_url}/{name}")

                    responses = await asyncio.gather(
                        *(make_request(name) for name in repeat("test-client", 5))
                    )
                    status_codes = [r.status_code for r in responses]
                    assert status_codes.count(200) == 3
                    assert status_codes.count(429) == 2


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.websocket
class TestWebSocketThrottle:
    async def test_websocket_throttle(self, backends: BackendGen) -> None:
        for backend in backends(persistent=False, namespace="ws_throttle_test"):
            async with backend(close_on_exit=True):
                throttle = WebSocketThrottle(
                    "test-websocket-throttle-inmemory-sl",
                    rate="3/5005ms",
                    identifier=default_client_identifier,
                    registry=ThrottleRegistry(),
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

                routes = [WebSocketRoute("/ws/", ws_endpoint)]
                app = Starlette(routes=routes)

                base_url = "http://0.0.0.0"
                running_loop = asyncio.get_running_loop()
                async with (
                    AsyncTestClient(
                        app=app,
                        base_url=base_url,
                        loop=running_loop,
                    ) as client,
                    client.websocket_connect(url="/ws/") as ws,
                ):
                    # Reset the backend before starting the test
                    # as connecting to the websocket already counts as a request
                    # and we want to start fresh.
                    await backend.reset()
                    await backend.initialize()

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
                                5.5  # For the last request, we wait a bit longer
                            )
                            await asyncio.sleep(sleep_time)

                    # Clear backend to ensure the throttle is cleared
                    await backend.reset()
                    await backend.initialize()

                    await asyncio.sleep(0.01)
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

    async def test_websocket_throttle_with_lifespan(
        self, backend: InMemoryBackend
    ) -> None:
        throttle = WebSocketThrottle(
            "test-ws-throttle-lifespan-sl",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": "PONG"})
            await websocket.close()

        routes = [WebSocketRoute("/ws/", ws_endpoint)]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        base_url = "http://0.0.0.0"
        async with AsyncTestClient(
            app=app,
            base_url=base_url,
            loop=asyncio.get_running_loop(),
        ) as client:
            # First request should succeed
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response == {"message": "PONG"}

            # Second request should also succeed
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response == {"message": "PONG"}

            # Third request should be throttled and disconnect
            try:
                async with client.websocket_connect(url="/ws/") as ws:
                    await ws.receive_json()
                    # Should not reach here
                    assert False
            except WebSocketDisconnect:
                # Expected - throttled connection
                pass

    async def test_websocket_throttle_exemption_with_unlimited_identifier(
        self, backend: InMemoryBackend
    ) -> None:
        throttle = WebSocketThrottle(
            "test-ws-throttle-exemption-sl",
            rate="2/s",
            identifier=unlimited_identifier,
            backend=backend,
            registry=ThrottleRegistry(),
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": "PONG"})
            await websocket.close()

        routes = [WebSocketRoute("/ws/", ws_endpoint)]
        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with AsyncTestClient(
            app=app,
            base_url=base_url,
            loop=running_loop,
        ) as client:
            # First request should succeed
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response == {"message": "PONG"}

            # Second request should also succeed
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response == {"message": "PONG"}

            # Third request should succeed (not throttled due to EXEMPTED identifier)
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response == {"message": "PONG"}

    async def test_disabled_websocket_throttle_skips_hit(
        self,
        backend: InMemoryBackend,
    ) -> None:
        """A disabled throttle should pass all connections through, even past the rate limit."""
        throttle = WebSocketThrottle(
            uid="dis-ws-skip-hit",
            rate="1/min",
            backend=backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            result = await throttle.hit(websocket)
            assert result is websocket
            await websocket.send_json({"ok": True})
            await websocket.close()

        routes = [WebSocketRoute("/ws/", ws_endpoint)]
        app = Starlette(routes=routes)

        base_url = "http://0.0.0.0"
        running_loop = asyncio.get_running_loop()
        async with AsyncTestClient(
            app=app,
            base_url=base_url,
            loop=running_loop,
        ) as client:
            # Fire 5 connections — all should succeed because the throttle is disabled
            for _ in range(5):
                async with client.websocket_connect(url="/ws/") as ws:
                    response = await ws.receive_json()
                    assert response == {"ok": True}

    @pytest.mark.concurrent
    async def test_websocket_throttle_concurrent(self, backends: BackendGen) -> None:
        for backend in backends(persistent=False, namespace="ws_throttle_concurrent"):
            async with backend(close_on_exit=True):
                throttle = WebSocketThrottle(
                    "ws-throttle-concurrent-sl",
                    rate="3/1050ms",
                    strategy=strategies.TokenBucketStrategy(),
                    registry=ThrottleRegistry(),
                )

                async def ws_endpoint(websocket: WebSocket) -> None:
                    await websocket.accept()
                    await throttle(websocket)
                    await websocket.send_json({"message": "PONG"})
                    await websocket.close()

                routes = [WebSocketRoute("/ws/", ws_endpoint)]
                app = Starlette(routes=routes)

                base_url = "http://0.0.0.0"
                running_loop = asyncio.get_running_loop()

                async def make_ws_connection() -> bool:
                    try:
                        async with (
                            AsyncTestClient(
                                app=app,
                                base_url=base_url,
                                loop=running_loop,
                            ) as client,
                            client.websocket_connect(url="/ws/") as ws,
                        ):
                            response = await ws.receive_json()
                            return response == {"message": "PONG"}
                    except WebSocketDisconnect:
                        return False

                results = await asyncio.gather(
                    *(make_ws_connection() for _ in range(5))
                )
                assert results.count(True) == 3
                assert results.count(False) == 2
