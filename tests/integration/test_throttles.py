"""
Tests for core `HTTPThrottle`/`WebSocketThrottle` behavior: initialization, enable/disable,
`update_*` methods, and end-to-end throttling across all backend implementations.
"""

import asyncio
import typing
from itertools import repeat

import anyio
import pytest
from httpx2 import Response
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.conftest import BackendGen
from tests.frameworks import (
    ASGIFramework,
    HTTPRoute,
    WSRoute,
)
from tests.utils import (
    default_client_identifier,
    make_client,
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


@pytest.mark.throttle
@requires_throttle_type
@pytest.mark.anyio
class TestThrottleBasic:
    async def test_throttle_initialization(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        with pytest.raises(ValueError):
            throttle_type(
                "test-init-1", rate=Rate(limit=-1), registry=ThrottleRegistry()
            )

        async def _throttle_handler(
            connection: HTTPConnection, wait_ms: float, *args, **kwargs
        ) -> None:
            return

        # Opened inline (not via a fixture) so the ambient backend context and the
        # throttle construction below run in the same task -- see module docstring
        # in test_throttle_cost.py for why that distinction matters here.
        async with inmemory_backend(close_on_exit=True):
            throttle = throttle_type(
                "test-init-2",
                rate=Rate(limit=2, milliseconds=10, seconds=50, minutes=2, hours=1),
                handle_throttled=_throttle_handler,
                registry=ThrottleRegistry(),
            )
            time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
            assert throttle.rate.expire == time_in_ms  # type: ignore[union-attr]
            assert throttle.backend is inmemory_backend
            assert throttle.identifier is inmemory_backend.identifier
            assert throttle.handle_throttled is not inmemory_backend.handle_throttled

    async def test_throttle_is_disabled_default_false(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-default",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        assert throttle.is_disabled is False

    async def test_throttle_disable_sets_flag(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-flag",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()
        assert throttle.is_disabled is True

    async def test_throttle_enable_clears_flag(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="dis-enable",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()
        await throttle.enable()
        assert throttle.is_disabled is False

    async def test_update_rate_string(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-rate-str",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.update_rate("100/s")
        assert throttle.rate == Rate.parse("100/s")
        assert throttle._uses_rate_func is False

    async def test_update_rate_rate_object(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-rate-obj",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        new_rate = Rate.parse("200/h")
        await throttle.update_rate(new_rate)
        assert throttle.rate is new_rate
        assert throttle._uses_rate_func is False

    async def test_update_rate_callable(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-rate-fn",
            rate="5/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )

        async def dynamic_rate(conn, ctx):
            return Rate.parse("50/min")

        await throttle.update_rate(dynamic_rate)
        assert throttle._uses_rate_func is True

    async def test_update_cost_static(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-cost",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.update_cost(3)
        assert throttle.cost == 3
        assert throttle._uses_cost_func is False

    async def test_update_cost_callable(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-cost-fn",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )

        async def dynamic_cost(conn, ctx):
            return 5

        await throttle.update_cost(dynamic_cost)
        assert throttle._uses_cost_func is True

    async def test_update_min_wait_period(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-mwp",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.update_min_wait_period(500)
        assert throttle.min_wait_period == 500
        await throttle.update_min_wait_period(None)
        assert throttle.min_wait_period is None

    async def test_update_identifier(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-ident",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )

        async def new_identifier(conn):
            return "custom-id"

        await throttle.update_identifier(new_identifier)
        assert throttle.identifier is new_identifier

    async def test_update_headers_none_clears(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-hdrs",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
            headers=DEFAULT_HEADERS_ALWAYS,
        )
        await throttle.update_headers(None)
        assert len(throttle._headers) == 0

    async def test_update_strategy(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-strategy",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        new_strategy = SlidingWindowLogStrategy()
        await throttle.update_strategy(new_strategy)
        assert throttle.strategy is new_strategy

    async def test_update_backend(
        self,
        inmemory_backend: InMemoryBackend,
        throttle_type: typing.Type[Throttle[HTTPConnection]],
    ) -> None:
        throttle = throttle_type(
            uid="upd-backend",
            rate="10/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        new_backend = InMemoryBackend(persistent=False)
        await throttle.update_backend(new_backend)
        assert throttle.backend is new_backend


@pytest.mark.throttle
@pytest.mark.anyio
async def test_disabled_http_throttle_skips_hit() -> None:
    """A disabled throttle passes all requests through, even past the rate limit.

    No app needed -- `.hit()` just needs *some* HTTPConnection to key off of.
    """
    throttle = HTTPThrottle(
        uid="dis-skip-hit",
        rate="1/min",
        backend=InMemoryBackend(),
        identifier=default_client_identifier,
    )
    await throttle.disable()

    req = Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/test",
            "query_string": b"",
            "headers": [],
        }
    )
    for _ in range(5):
        result = await throttle.hit(req)
        assert result is req


@pytest.mark.throttle
@pytest.mark.anyio
class TestHTTPThrottle:
    async def test_http_throttle(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        for backend in backends(persistent=False, namespace="http_throttle_test"):
            async with backend(close_on_exit=True):
                throttle = HTTPThrottle(
                    f"test-http-throttle-{web_framework.name}",
                    rate="3/3500ms",
                    registry=ThrottleRegistry(),
                )
                sleep_time = 3.5

                async def endpoint(request: Request) -> JSONResponse:
                    await throttle(request)
                    name = request.path_params.get("name", "unknown")
                    return JSONResponse({"message": f"PONG: {name}"})

                app = web_framework.build_app(
                    http_routes=[HTTPRoute("/{name}", endpoint)]
                )

                async with make_client(app, base_url="http://test") as client:
                    for count, name in enumerate(repeat("test-client", 5), start=1):
                        if count == 4:
                            await anyio.sleep(sleep_time)
                        response = await client.get(f"/{name}")
                        assert response.status_code == 200
                        assert response.json() == {"message": f"PONG: {name}"}

                    await backend.reset()
                    await backend.initialize()
                    for count, name in enumerate(repeat("test-client", 5), start=1):
                        response = await client.get(f"/{name}")
                        if count > 3:
                            assert response.status_code == 429
                            assert response.headers["Retry-After"] is not None
                        else:
                            assert response.status_code == 200

    async def test_http_throttle_with_lifespan(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """
        Sync test, `starlette.testclient.TestClient` -- needs the app's lifespan
        to actually run so the throttle can resolve its backend, which neither
        `httpx2.AsyncClient`+`ASGITransport` nor `make_client` do for plain
        HTTP (see module docstring in test_throttle_dynamic_backend.py).
        """
        throttle = HTTPThrottle(
            f"test-throttle-app-lifespan-{web_framework.name}",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "PONG"})

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/", endpoint)], lifespan=inmemory_backend.lifespan
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            for _ in range(2):
                response = await client.get("/")
                assert response.status_code == 200
                assert response.json() == {"message": "PONG"}

            response = await client.get("/")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None

    async def test_http_throttle_exemption_with_unlimited_identifier(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            f"test-throttle-exemption-{web_framework.name}",
            rate="2/s",
            identifier=unlimited_identifier,
            backend=inmemory_backend,
            registry=ThrottleRegistry(),
        )

        async def endpoint(request: Request) -> JSONResponse:
            await throttle(request)
            return JSONResponse({"message": "PONG"})

        app = web_framework.build_app(http_routes=[HTTPRoute("/", endpoint)])

        async with make_client(app, base_url="http://test") as client:
            for _ in range(2):
                response = await client.get("/")
                assert response.status_code == 200
                assert response.json() == {"message": "PONG"}

            # 3rd would normally be throttled, but EXEMPTED identifiers bypass it
            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers.get("Retry-After", None) is None

    @pytest.mark.concurrent
    async def test_http_throttle_concurrent(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        for backend in backends(persistent=False, namespace="http_throttle_concurrent"):
            async with backend(close_on_exit=True):
                throttle = HTTPThrottle(
                    f"http-throttle-concurrent-{web_framework.name}",
                    rate="3/s",
                    strategy=strategies.TokenBucketStrategy(),
                    registry=ThrottleRegistry(),
                )

                async def endpoint(request: Request) -> JSONResponse:
                    await throttle(request)
                    name = request.path_params.get("name", "unknown")
                    return JSONResponse({"message": f"PONG: {name}"})

                app = web_framework.build_app(
                    http_routes=[HTTPRoute("/{name}", endpoint)]
                )

                async with make_client(app, base_url="http://test") as client:

                    async def make_request(name: str) -> Response:
                        return await client.get(f"/{name}")

                    responses = await asyncio.gather(
                        *(make_request(name) for name in repeat("test-client", 5))
                    )
                    status_codes = [r.status_code for r in responses]
                    assert status_codes.count(200) == 3
                    assert status_codes.count(429) == 2


@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.anyio
class TestWebSocketThrottle:
    async def test_websocket_throttle(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        for backend in backends(persistent=False, namespace="ws_throttle_test"):
            async with backend(close_on_exit=True):
                throttle = WebSocketThrottle(
                    f"test-websocket-throttle-{web_framework.name}",
                    rate="3/5005ms",
                    identifier=default_client_identifier,
                    registry=ThrottleRegistry(),
                )

                async def ws_endpoint(websocket: WebSocket) -> None:
                    await websocket.accept()
                    close_code, close_reason = 1000, "Normal closure"
                    while True:
                        try:
                            data = await websocket.receive_json()
                            await throttle(websocket)
                            await websocket.send_json(
                                {
                                    "status": "success",
                                    "status_code": 200,
                                    "detail": "Request successful",
                                    "data": data,
                                }
                            )
                        except HTTPException as exc:
                            await websocket.send_json(
                                {
                                    "status": "error",
                                    "status_code": exc.status_code,
                                    "detail": exc.detail,
                                    "data": None,
                                }
                            )
                            close_reason = exc.detail
                            break
                        except Exception:
                            await websocket.send_json(
                                {
                                    "status": "error",
                                    "status_code": 500,
                                    "detail": "Operation failed",
                                    "data": None,
                                }
                            )
                            close_code, close_reason = 1011, "Internal error"
                            break

                    # Let the last message flush before closing.
                    await asyncio.sleep(1)
                    await websocket.close(code=close_code, reason=close_reason)

                app = web_framework.build_app(ws_routes=[WSRoute("/ws/", ws_endpoint)])

                base_url = "http://0.0.0.0"
                running_loop = asyncio.get_running_loop()
                async with (
                    make_client(app, base_url=base_url, loop=running_loop) as client,
                    client.websocket_connect(url="/ws/") as ws,
                ):
                    # Connecting already counts as a request -- reset for a clean budget.
                    await backend.reset()
                    await backend.initialize()

                    async def make_ws_request() -> typing.Tuple[str, int]:
                        try:
                            await ws.send_json({"message": "ping"})
                            response = await ws.receive_json()
                            return response["status"], response["status_code"]
                        except WebSocketDisconnect:
                            return "disconnected", 1000

                    for count in range(1, 6):
                        result = await make_ws_request()
                        assert result[0] == "success"
                        assert result[1] == 200
                        if count == 3:
                            await asyncio.sleep(5.5)

                    await backend.reset()
                    await backend.initialize()
                    await asyncio.sleep(0.01)

                    for count in range(1, 4):
                        result = await make_ws_request()
                        if count > 3:
                            assert result[0] == "error"
                            assert result[1] == 429
                        else:
                            assert result[0] == "success"
                            assert result[1] == 200

    async def test_websocket_throttle_with_lifespan(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = WebSocketThrottle(
            f"test-ws-throttle-lifespan-{web_framework.name}",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": "PONG"})
            await websocket.close()

        app = web_framework.build_app(
            ws_routes=[WSRoute("/ws/", ws_endpoint)], lifespan=inmemory_backend.lifespan
        )

        async with make_client(
            app, base_url="http://0.0.0.0", loop=asyncio.get_running_loop()
        ) as client:
            for _ in range(2):
                async with client.websocket_connect(url="/ws/") as ws:
                    response = await ws.receive_json()
                    assert response == {"message": "PONG"}

            # 3rd connection is throttled -- sent a rate_limit message, not dropped.
            async with client.websocket_connect(url="/ws/") as ws:
                response = await ws.receive_json()
                assert response["type"] == "rate_limit"
                assert "retry_after" in response

    async def test_websocket_throttle_exemption_with_unlimited_identifier(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = WebSocketThrottle(
            f"test-ws-throttle-exemption-{web_framework.name}",
            rate="2/s",
            identifier=unlimited_identifier,
            backend=inmemory_backend,
            registry=ThrottleRegistry(),
        )

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"message": "PONG"})
            await websocket.close()

        app = web_framework.build_app(ws_routes=[WSRoute("/ws/", ws_endpoint)])

        async with make_client(
            app, base_url="http://0.0.0.0", loop=asyncio.get_running_loop()
        ) as client:
            for _ in range(3):
                async with client.websocket_connect(url="/ws/") as ws:
                    response = await ws.receive_json()
                    assert response == {"message": "PONG"}

    async def test_disabled_websocket_throttle_skips_hit(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """A disabled throttle passes all connections through, even past the limit."""
        throttle = WebSocketThrottle(
            uid=f"dis-ws-skip-hit-{web_framework.name}",
            rate="1/min",
            backend=inmemory_backend,
            identifier=default_client_identifier,
        )
        await throttle.disable()

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            result = await throttle.hit(websocket)
            assert result is websocket
            await websocket.send_json({"ok": True})
            await websocket.close()

        app = web_framework.build_app(ws_routes=[WSRoute("/ws/", ws_endpoint)])

        async with make_client(
            app, base_url="http://0.0.0.0", loop=asyncio.get_running_loop()
        ) as client:
            for _ in range(5):
                async with client.websocket_connect(url="/ws/") as ws:
                    response = await ws.receive_json()
                    assert response == {"ok": True}

    @pytest.mark.concurrent
    async def test_websocket_throttle_concurrent(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        for backend in backends(persistent=False, namespace="ws_throttle_concurrent"):
            async with backend(close_on_exit=True):
                throttle = WebSocketThrottle(
                    f"ws-throttle-concurrent-{web_framework.name}",
                    rate="3/1050ms",
                    strategy=strategies.TokenBucketStrategy(),
                    registry=ThrottleRegistry(),
                )

                async def ws_endpoint(websocket: WebSocket) -> None:
                    await websocket.accept()
                    await throttle(websocket)
                    await websocket.send_json({"message": "PONG"})
                    await websocket.close()

                app = web_framework.build_app(ws_routes=[WSRoute("/ws/", ws_endpoint)])

                base_url = "http://0.0.0.0"
                running_loop = asyncio.get_running_loop()

                async def make_ws_connection() -> bool:
                    nonlocal app, base_url, running_loop
                    try:
                        async with (
                            make_client(
                                app, base_url=base_url, loop=running_loop
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
