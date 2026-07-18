"""
Tests for `traffik.middleware` (`MiddlewareThrottle`, `ThrottleMiddleware`, `_prep_throttles`).
"""

import asyncio
import re
import typing

import pytest
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.conftest import BackendGen
from tests.frameworks import ASGIFramework, HTTPRoute, WSRoute
from tests.utils import default_client_identifier, make_client
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware, _prep_throttles
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
class TestMiddlewareThrottleFiltering:
    """`MiddlewareThrottle` filter mechanics"""

    async def test_initialization(self) -> None:
        throttle = HTTPThrottle(
            uid="test-mw-throttle-init",
            rate="5/min",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, path="/api/", methods={"GET", "POST"}
        )
        assert isinstance(middleware_throttle.rule.path, re.Pattern)
        assert middleware_throttle.rule.methods is not None
        assert (
            "get" in middleware_throttle.rule.methods
            or "GET" in middleware_throttle.rule.methods
        )
        assert (
            "post" in middleware_throttle.rule.methods
            or "POST" in middleware_throttle.rule.methods
        )
        assert middleware_throttle.rule.predicate is None

        regex_pattern = re.compile(r"/api/\d+")
        middleware_throttle_regex = MiddlewareThrottle(
            throttle=throttle, path=regex_pattern, methods={"GET"}
        )
        assert middleware_throttle_regex.rule.path is regex_pattern

        middleware_throttle_all = MiddlewareThrottle(throttle=throttle)
        assert middleware_throttle_all.rule.path is None
        assert middleware_throttle_all.rule.methods is None
        assert middleware_throttle_all.rule.predicate is None

    async def test_method_filtering(self, inmemory_backend: InMemoryBackend) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="method-filter-test",
                rate="1/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(throttle=throttle, methods={"GET"})

            get_request = Request({"type": "http", "method": "GET", "path": "/test"})
            post_request = Request({"type": "http", "method": "POST", "path": "/test"})

            assert await middleware_throttle(get_request) is get_request
            assert await middleware_throttle(post_request) is post_request

    async def test_path_filtering(self, inmemory_backend: InMemoryBackend) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="path-filter-test",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(throttle=throttle, path="/api/")

            api_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/users",
                }
            )
            public_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/public/info",
                }
            )

            assert await middleware_throttle(api_request) is api_request
            assert await middleware_throttle(public_request) is public_request

    async def test_regex_path_filtering(
        self, inmemory_backend: InMemoryBackend
    ) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="regex-path-test",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(
                throttle=throttle, path=r"/api/\d+"
            )

            for path in ("/api/123", "/api/456", "/api/abc", "/api/", "/public/123"):
                request = Request({"type": "http", "method": "GET", "path": path})
                assert await middleware_throttle(request) is request

    async def test_predicate_filtering(self, inmemory_backend: InMemoryBackend) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="predicate-filter-test",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )

            async def is_premium_user(connection: HTTPConnection) -> bool:
                return (
                    connection.scope.get("headers", {}).get("x-user-tier") == "premium"
                )

            middleware_throttle = MiddlewareThrottle(
                throttle=throttle, predicate=is_premium_user
            )

            premium_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/test",
                    "headers": {"x-user-tier": "premium"},
                }
            )
            free_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/test",
                    "headers": {"x-user-tier": "free"},
                }
            )

            assert await middleware_throttle(premium_request) is premium_request
            assert await middleware_throttle(free_request) is free_request

    async def test_combined_filters(self, inmemory_backend: InMemoryBackend) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="combined-filter-test",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )

            async def is_authorized(connection: HTTPConnection) -> bool:
                headers = dict(connection.scope.get("headers", []))
                return b"authorization" in headers

            middleware_throttle = MiddlewareThrottle(
                throttle=throttle,
                path="/api/",
                methods={"POST"},
                predicate=is_authorized,
            )

            test_cases = [
                ("POST", "/api/users", True),
                ("GET", "/api/users", True),
                ("POST", "/public/users", True),
                ("POST", "/api/users", False),
                ("GET", "/public/users", False),
            ]
            for method, path, has_auth in test_cases:
                headers = [(b"authorization", b"Bearer token")] if has_auth else []
                request = Request(
                    {
                        "type": "http",
                        "method": method,
                        "path": path,
                        "headers": headers,
                    }
                )
                assert await middleware_throttle(request) is request


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
class TestMiddlewareThrottleRegexMatching:
    """More `path=` regex edge cases - still no app needed."""

    async def test_complex_regex_patterns(
        self, inmemory_backend: InMemoryBackend
    ) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="complex-regex",
                rate="3/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            uuid_pattern = re.compile(
                r"/api/(?:users|products)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}"
            )
            middleware_throttle = MiddlewareThrottle(
                throttle=throttle, path=uuid_pattern
            )

            for path in (
                "/api/users/550e8400-e29b-41d4-a716-446655440000",
                "/api/products/123e4567-e89b-12d3-a456-426614174000",
                "/api/users/123",
                "/api/orders/550e8400-e29b-41d4-a716-446655440000",
                "/api/users/not-a-uuid",
            ):
                request = Request({"type": "http", "method": "GET", "path": path})
                assert await middleware_throttle(request) is request

    async def test_string_auto_compile_to_regex(
        self, inmemory_backend: InMemoryBackend
    ) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="auto-compile",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(throttle=throttle, path="/api/")
            assert isinstance(middleware_throttle.rule.path, re.Pattern)
            assert middleware_throttle.rule.path.pattern == "/api/"

            matching = Request({"type": "http", "method": "GET", "path": "/api/users"})
            non_matching = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/public/data",
                }
            )
            assert await middleware_throttle(matching) is matching
            assert await middleware_throttle(non_matching) is non_matching

    async def test_regex_with_query_params_ignored(
        self, inmemory_backend: InMemoryBackend
    ) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="query-ignore",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(
                throttle=throttle, path=r"^/api/search$"
            )

            for _ in range(2):
                request = Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/search",
                    }
                )
                assert await middleware_throttle(request) is request

            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/search/results",
                }
            )
            assert await middleware_throttle(request) is request

    async def test_case_sensitive_regex(
        self, inmemory_backend: InMemoryBackend
    ) -> None:
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid="case-sensitive",
                rate="3/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(
                throttle=throttle, path=re.compile(r"/API/")
            )
            for path in ("/API/users", "/api/users", "/Api/users"):
                request = Request({"type": "http", "method": "GET", "path": path})
                assert await middleware_throttle(request) is request


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
class TestThrottleMiddlewareBasic:
    async def test_basic_functionality(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"middleware-basic-{web_framework.name}",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, path="/api/", methods={"GET"}
        )

        async def api_data(request: Request) -> JSONResponse:
            return JSONResponse({"data": "response"})

        async def public_data(request: Request) -> JSONResponse:
            return JSONResponse({"data": "public"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/api/data", api_data),
                HTTPRoute("/public/data", public_data),
            ],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            assert (await client.get("/api/data")).status_code == 200
            assert (await client.get("/api/data")).status_code == 200

            response3 = await client.get("/api/data")
            assert response3.status_code == 429
            assert "Retry-After" in response3.headers

            assert (await client.get("/public/data")).status_code == 200

    async def test_multiple_throttles(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        api_throttle = HTTPThrottle(
            uid=f"api-throttle-{web_framework.name}",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        admin_throttle = HTTPThrottle(
            uid=f"admin-throttle-{web_framework.name}",
            rate="1/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttles = [
            MiddlewareThrottle(api_throttle, path="/api/", methods={"GET"}),
            MiddlewareThrottle(admin_throttle, path="/admin/", methods={"POST"}),
        ]

        async def get_users(request: Request) -> JSONResponse:
            return JSONResponse({"users": []})

        async def update_settings(request: Request) -> JSONResponse:
            return JSONResponse({"status": "updated"})

        async def get_info(request: Request) -> JSONResponse:
            return JSONResponse({"info": "public"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/api/users", get_users),
                HTTPRoute("/admin/settings", update_settings, methods=["POST"]),
                HTTPRoute("/public/info", get_info),
            ],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=middleware_throttles,
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            assert (await client.get("/api/users")).status_code == 200
            assert (await client.get("/api/users")).status_code == 200
            assert (await client.get("/api/users")).status_code == 429

            assert (await client.post("/admin/settings")).status_code == 200
            assert (await client.post("/admin/settings")).status_code == 429

            assert (await client.get("/public/info")).status_code == 200

    async def test_method_specificity(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"method-specific-{web_framework.name}",
            rate="1/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, path="/api/data", methods={"POST"}
        )

        async def get_data(request: Request) -> JSONResponse:
            return JSONResponse({"method": "GET"})

        async def post_data(request: Request) -> JSONResponse:
            return JSONResponse({"method": "POST"})

        async def put_data(request: Request) -> JSONResponse:
            return JSONResponse({"method": "PUT"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/api/data", get_data),
                HTTPRoute("/api/data", post_data, methods=["POST"]),
                HTTPRoute("/api/data", put_data, methods=["PUT"]),
            ],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            for _ in range(3):
                assert (await client.get("/api/data")).status_code == 200

            assert (await client.post("/api/data")).status_code == 200
            assert (await client.post("/api/data")).status_code == 429

            assert (await client.put("/api/data")).status_code == 200
            assert (await client.put("/api/data")).status_code == 200

    async def test_with_predicate(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"predicate-middleware-{web_framework.name}",
            rate="1/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def premium_only_predicate(connection: HTTPConnection) -> bool:
            return dict(connection.headers).get("x-user-tier") == "premium"

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, predicate=premium_only_predicate
        )

        async def get_data(request: Request) -> JSONResponse:
            return JSONResponse({"data": "response"})

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/data", get_data)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            free_headers = {"x-user-tier": "free"}
            for _ in range(3):
                assert (
                    await client.get("/data", headers=free_headers)
                ).status_code == 200

            premium_headers = {"x-user-tier": "premium"}
            assert (
                await client.get("/data", headers=premium_headers)
            ).status_code == 200
            assert (
                await client.get("/data", headers=premium_headers)
            ).status_code == 429

    async def test_no_backend_specified(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """`ThrottleMiddleware` with no `backend=` falls back to the lifespan one."""
        throttle = HTTPThrottle(
            uid=f"no-backend-{web_framework.name}",
            rate="1/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(throttle=throttle)

        async def test_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"test": "response"})

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/test", test_endpoint)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware, middleware_throttles=[middleware_throttle]
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            assert (await client.get("/test")).status_code == 200
            assert (await client.get("/test")).status_code == 429

    async def test_exemption_with_predicate(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"exemption-{web_framework.name}",
            rate=Rate.parse("1/s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def non_admin_predicate(connection: HTTPConnection) -> bool:
            return dict(connection.headers).get("x-user-role") != "admin"

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, predicate=non_admin_predicate
        )

        async def get_data(request: Request) -> JSONResponse:
            return JSONResponse({"data": "response"})

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/data", get_data)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            admin_headers = {"x-user-role": "admin"}
            for _ in range(5):
                assert (
                    await client.get("/data", headers=admin_headers)
                ).status_code == 200

            user_headers = {"x-user-role": "user"}
            assert (await client.get("/data", headers=user_headers)).status_code == 200
            assert (await client.get("/data", headers=user_headers)).status_code == 429

    async def test_case_insensitive_methods(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"case-insensitive-{web_framework.name}",
            rate=Rate.parse("1/1s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, methods={"GET", "post", "Put"}
        )

        async def get_test(request: Request) -> JSONResponse:
            return JSONResponse({"method": "GET"})

        async def post_test(request: Request) -> JSONResponse:
            return JSONResponse({"method": "POST"})

        async def put_test(request: Request) -> JSONResponse:
            return JSONResponse({"method": "PUT"})

        async def delete_test(request: Request) -> JSONResponse:
            return JSONResponse({"method": "DELETE"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/test", get_test),
                HTTPRoute("/test", post_test, methods=["POST"]),
                HTTPRoute("/test", put_test, methods=["PUT"]),
                HTTPRoute("/test", delete_test, methods=["DELETE"]),
            ],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            assert (await client.get("/test")).status_code == 200
            assert (await client.get("/test")).status_code == 429

            assert (await client.post("/test")).status_code == 200
            assert (await client.post("/test")).status_code == 429

            assert (await client.put("/test")).status_code == 200
            assert (await client.put("/test")).status_code == 429

            # DELETE isn't in the methods set - never throttled.
            assert (await client.delete("/test")).status_code == 200
            assert (await client.delete("/test")).status_code == 200

    async def test_with_no_throttles(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        async def test_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"test": "response"})

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/test", test_endpoint)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            for _ in range(10):
                assert (await client.get("/test")).status_code == 200

    async def test_multiple_overlapping_patterns(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        general_throttle = HTTPThrottle(
            uid=f"general-throttle-{web_framework.name}",
            rate=Rate.parse("5/s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        specific_throttle = HTTPThrottle(
            uid=f"specific-throttle-{web_framework.name}",
            rate=Rate.parse("2/s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttles = [
            MiddlewareThrottle(general_throttle, path="/api/"),
            MiddlewareThrottle(specific_throttle, path="/api/users/"),
        ]

        async def api_general(request: Request) -> JSONResponse:
            return JSONResponse({"type": "general"})

        async def api_users(request: Request) -> JSONResponse:
            return JSONResponse({"type": "users"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/api/data", api_general),
                HTTPRoute("/api/users/list", api_users),
            ],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=middleware_throttles,
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            # /api/data only matches the general throttle (limit=5)
            for i in range(5):
                response = await client.get("/api/data")
                assert response.status_code == 200, f"Request {i + 1} should succeed"
            assert (await client.get("/api/data")).status_code == 429

            # /api/users/list matches BOTH - hits the more restrictive one (2)
            for i in range(2):
                response = await client.get("/api/users/list")
                assert response.status_code == 200, (
                    f"Users request {i + 1} should succeed"
                )
            assert (await client.get("/api/users/list")).status_code == 429


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.websocket
@pytest.mark.anyio
class TestThrottleMiddlewareWebSocket:
    async def test_websocket_passthrough(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Middleware throttles HTTP but leaves an unthrottled WS route alone."""
        throttle = HTTPThrottle(
            uid=f"ws-passthrough-{web_framework.name}",
            rate=Rate.parse("1/1s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(throttle=throttle)

        async def http_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"type": "http"})

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "connected"})
            await websocket.close()

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/http", http_endpoint)],
            ws_routes=[WSRoute("/ws", ws_endpoint)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            assert (await client.get("/http")).status_code == 200
            assert (await client.get("/http")).status_code == 429

            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    assert await ws.receive_json() == {"message": "connected"}

    async def test_websocket_throttle(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        ws_throttle = WebSocketThrottle(
            uid=f"ws-middleware-throttle-{web_framework.name}",
            rate=Rate.parse("2/5s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(throttle=ws_throttle, path="/ws")

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"message": "connected"})
            await websocket.close()

        app = web_framework.build_app(
            ws_routes=[WSRoute("/ws", ws_endpoint)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    assert await ws.receive_json() == {"message": "connected"}

            # 3rd connection is rejected before accept - fails on connect itself.
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws") as ws:
                    await ws.receive_json()

    async def test_mixed_http_and_websocket_throttles(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        http_throttle = HTTPThrottle(
            uid=f"mixed-http-{web_framework.name}",
            rate=Rate.parse("2/5s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        ws_throttle = WebSocketThrottle(
            uid=f"mixed-ws-{web_framework.name}",
            rate=Rate.parse("2/5s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        async def http_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"type": "http"})

        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"type": "websocket"})
            await websocket.close()

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/http", http_endpoint)],
            ws_routes=[WSRoute("/ws", ws_endpoint)],
            lifespan=inmemory_backend.lifespan,
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[
                        MiddlewareThrottle(throttle=http_throttle),
                        MiddlewareThrottle(throttle=ws_throttle),
                    ],
                    backend=inmemory_backend,
                )
            ],
        )

        async with make_client(app, base_url="http://0.0.0.0") as client:
            # HTTP and WS throttles are independent of each other.
            assert (await client.get("/http")).status_code == 200
            assert (await client.get("/http")).status_code == 200
            assert (await client.get("/http")).status_code == 429

            for _ in range(2):
                async with client.websocket_connect("/ws") as ws:
                    assert await ws.receive_json() == {"type": "websocket"}

            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws") as ws:
                    await ws.receive_json()


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
class TestThrottleMiddlewareBackends:
    async def test_multiple_backends(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        for backend in backends(persistent=False, namespace="middleware_test"):
            throttle = HTTPThrottle(
                uid=f"backend-middleware-{web_framework.name}",
                rate="2/min",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
            )
            middleware_throttle = MiddlewareThrottle(throttle=throttle, path="/api/")

            async with backend(close_on_exit=True):

                async def test_endpoint(request: Request) -> JSONResponse:
                    return JSONResponse({"backend": "test"})

                async def public_endpoint(request: Request) -> JSONResponse:
                    return JSONResponse({"public": "test"})

                app = web_framework.build_app(
                    http_routes=[
                        HTTPRoute("/api/test", test_endpoint),
                        HTTPRoute("/public/test", public_endpoint),
                    ],
                    middleware=[
                        Middleware(
                            ThrottleMiddleware,
                            middleware_throttles=[middleware_throttle],
                            backend=backend,
                        )
                    ],
                )

                async with make_client(app, base_url="http://0.0.0.0") as client:
                    assert (await client.get("/api/test")).status_code == 200
                    assert (await client.get("/api/test")).status_code == 200

                    response3 = await client.get("/api/test")
                    assert response3.status_code == 429
                    assert "Retry-After" in response3.headers

                    assert (await client.get("/public/test")).status_code == 200

    @pytest.mark.concurrent
    async def test_concurrent_requests(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"concurrent-middleware-{web_framework.name}",
            rate=Rate.parse("3/5s"),
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(throttle=throttle)

        async with inmemory_backend(close_on_exit=True):

            async def concurrent_endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"concurrent": "test"})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/concurrent", concurrent_endpoint)],
                middleware=[
                    Middleware(
                        ThrottleMiddleware,
                        middleware_throttles=[middleware_throttle],
                        backend=inmemory_backend,
                    )
                ],
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:

                async def make_request():
                    return await client.get("/concurrent")

                responses = await asyncio.gather(*(make_request() for _ in range(10)))
                status_codes = [r.status_code for r in responses]
                assert status_codes.count(200) == 3
                assert status_codes.count(429) == 7


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
class TestThrottleMiddlewareStreaming:
    async def test_with_streaming_responses(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """
        Throttling is checked before streaming starts: allowed requests stream
        normally, a throttled request gets a plain 429 with no chunks at all.
        """
        throttle = HTTPThrottle(
            uid=f"streaming-{web_framework.name}",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, path="/api/stream", methods={"GET"}
        )

        async def stream_generator():
            for i in range(5):
                yield f"chunk-{i}\n".encode()
                await asyncio.sleep(0.01)

        async def stream_endpoint(request: Request) -> StreamingResponse:
            return StreamingResponse(
                stream_generator(),
                media_type="text/plain",
                headers={"X-Custom-Header": "streaming"},
            )

        async def regular_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"data": "regular"})

        app = web_framework.build_app(
            http_routes=[
                HTTPRoute("/api/stream", stream_endpoint),
                HTTPRoute("/api/regular", regular_endpoint),
            ],
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with (
            inmemory_backend(close_on_exit=True),
            make_client(app, base_url="http://testserver") as client,
        ):
            response1 = await client.get("/api/stream")
            assert response1.status_code == 200
            assert response1.headers["X-Custom-Header"] == "streaming"
            for i in range(5):
                assert f"chunk-{i}" in response1.text

            response2 = await client.get("/api/stream")
            assert response2.status_code == 200

            response3 = await client.get("/api/stream")
            assert response3.status_code == 429
            assert "Retry-After" in response3.headers
            assert "Too many requests" in response3.text
            assert "chunk-" not in response3.text

            response4 = await client.get("/api/regular")
            assert response4.status_code == 200
            assert response4.json() == {"data": "regular"}

    async def test_streaming_with_large_chunks(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        throttle = HTTPThrottle(
            uid=f"large-stream-{web_framework.name}",
            rate="10/m",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle, path="/api/download"
        )

        async def large_stream_generator():
            chunk_size = 1024
            for chunk_num in range(10):
                chunk_header = f"CHUNK{chunk_num:04d}".encode()
                padding = b"X" * (chunk_size - len(chunk_header))
                yield chunk_header + padding
                await asyncio.sleep(0.001)

        async def download_endpoint(request: Request) -> StreamingResponse:
            return StreamingResponse(
                large_stream_generator(),
                media_type="application/octet-stream",
                headers={"Content-Disposition": "attachment; filename=data.bin"},
            )

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/api/download", download_endpoint)],
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with (
            inmemory_backend(close_on_exit=True),
            make_client(app, base_url="http://testserver") as client,
        ):
            response = await client.get("/api/download")
            assert response.status_code == 200
            assert "Content-Disposition" in response.headers

            content = response.content
            assert len(content) == 10 * 1024
            for chunk_num in range(10):
                assert f"CHUNK{chunk_num:04d}".encode() in content

    async def test_streaming_exception_during_stream(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Throttling is checked before streaming - an in-stream error is unrelated."""
        throttle = HTTPThrottle(
            uid=f"stream-exception-{web_framework.name}",
            rate="5/m",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )
        middleware_throttle = MiddlewareThrottle(throttle=throttle)

        async def failing_stream_generator():
            yield b"chunk-1\n"
            yield b"chunk-2\n"
            raise ValueError("Streaming error")

        async def failing_stream_endpoint(request: Request) -> StreamingResponse:
            return StreamingResponse(
                failing_stream_generator(), media_type="text/plain"
            )

        app = web_framework.build_app(
            http_routes=[HTTPRoute("/stream", failing_stream_endpoint)],
            middleware=[
                Middleware(
                    ThrottleMiddleware,
                    middleware_throttles=[middleware_throttle],
                    backend=inmemory_backend,
                )
            ],
        )

        async with (
            inmemory_backend(close_on_exit=True),
            make_client(app, base_url="http://testserver") as client,
        ):
            with pytest.raises(ValueError, match="Streaming error"):
                await client.get("/stream")


def make_http_throttle(
    uid: str, cost: typing.Optional[int] = None
) -> MiddlewareThrottle:
    return MiddlewareThrottle(
        throttle=HTTPThrottle(
            uid=uid,
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        ),
        cost=cost,
    )


def make_ws_throttle(uid: str, cost: typing.Optional[int] = None) -> MiddlewareThrottle:
    return MiddlewareThrottle(
        throttle=WebSocketThrottle(
            uid=uid,
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        ),
        cost=cost,
    )


@pytest.mark.throttle
@pytest.mark.middleware
class TestPrepThrottles:
    """`_prep_throttles` - pure function, no app, no framework."""

    def test_cheap_first(self) -> None:
        cheap_throttle = make_http_throttle("cheap", cost=1)
        t_mid = make_http_throttle("mid", cost=5)
        expensive_throttle = make_http_throttle("expensive", cost=10)
        result = _prep_throttles(
            [expensive_throttle, cheap_throttle, t_mid], sort="cheap_first"
        )
        assert result["http"] == [cheap_throttle, t_mid, expensive_throttle]

    def test_cheap_last(self) -> None:
        cheap_throttle = make_http_throttle("cheap", cost=1)
        t_mid = make_http_throttle("mid", cost=5)
        expensive_throttle = make_http_throttle("expensive", cost=10)
        result = _prep_throttles(
            [cheap_throttle, t_mid, expensive_throttle], sort="cheap_last"
        )
        assert result["http"] == [expensive_throttle, t_mid, cheap_throttle]

    def test_no_sort(self) -> None:
        t1 = make_http_throttle("first", cost=10)
        t2 = make_http_throttle("second", cost=1)
        t3 = make_http_throttle("third", cost=5)
        for sort_val in (False, None):
            result = _prep_throttles([t1, t2, t3], sort=sort_val)
            assert result["http"] == [t1, t2, t3]

    def test_custom_callable(self) -> None:
        t1 = make_http_throttle("alpha", cost=5)
        t2 = make_http_throttle("beta", cost=1)
        t3 = make_http_throttle("gamma", cost=10)
        result = _prep_throttles([t3, t1, t2], sort=lambda t: t.throttle.uid)  # type: ignore
        assert result["http"] == [t1, t2, t3]

    def test_none_cost_sorted_last(self) -> None:
        """
        `MiddlewareThrottle(cost=None)` falls back to the wrapped throttle's
        cost (default 1). `t_no_cost` gets the same sort key as `cheap_throttle`
        (1, False); stable sort keeps `t_no_cost` (input index 0) ahead of
        `cheap_throttle` (index 2).
        """
        cheap_throttle = make_http_throttle("cheap", cost=1)
        t_no_cost = make_http_throttle("no-cost", cost=None)
        expensive_throttle = make_http_throttle("expensive", cost=100)
        result = _prep_throttles(
            [t_no_cost, expensive_throttle, cheap_throttle], sort="cheap_first"
        )
        assert result["http"] == [t_no_cost, cheap_throttle, expensive_throttle]

    def test_none_cost_sorted_first_with_cheap_last(self) -> None:
        cheap_throttle = make_http_throttle("cheap", cost=1)
        t_no_cost = make_http_throttle("no-cost", cost=None)
        expensive_throttle = make_http_throttle("expensive", cost=100)
        result = _prep_throttles(
            [cheap_throttle, expensive_throttle, t_no_cost], sort="cheap_last"
        )
        assert result["http"] == [expensive_throttle, cheap_throttle, t_no_cost]

    def test_invalid_sort(self) -> None:
        t = make_http_throttle("test", cost=1)
        with pytest.raises(ValueError, match="Invalid value for `sort`"):
            _prep_throttles([t], sort="invalid")  # type: ignore[arg-type]

    def test_categorization(self) -> None:
        t_http1 = make_http_throttle("http1", cost=1)
        t_http2 = make_http_throttle("http2", cost=2)
        t_ws1 = make_ws_throttle("ws1", cost=1)
        t_ws2 = make_ws_throttle("ws2", cost=2)
        result = _prep_throttles([t_ws2, t_http2, t_ws1, t_http1], sort="cheap_first")
        assert result["http"] == [t_http1, t_http2]
        assert result["websocket"] == [t_ws1, t_ws2]


@pytest.mark.throttle
@pytest.mark.middleware
@pytest.mark.anyio
async def test_middleware_sort_parameter_integration(
    inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
) -> None:
    """The `sort=` param on `ThrottleMiddleware` actually reorders `middleware_throttles`."""
    expensive_throttle = MiddlewareThrottle(
        throttle=HTTPThrottle(
            uid=f"expensive-{web_framework.name}",
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        ),
        cost=10,
    )
    cheap_throttle = MiddlewareThrottle(
        throttle=HTTPThrottle(
            uid=f"cheap-{web_framework.name}",
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        ),
        cost=1,
    )

    async def endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def _find_throttle_middleware(app) -> typing.Optional[ThrottleMiddleware]:
        """Walk the built middleware stack to find the `ThrottleMiddleware` layer."""
        layer = app.middleware_stack
        while layer is not None and not isinstance(layer, ThrottleMiddleware):
            layer = getattr(layer, "app", None)
        return layer  # type: ignore[return-value]

    app1 = web_framework.build_app(
        http_routes=[HTTPRoute("/test", endpoint)],
        lifespan=inmemory_backend.lifespan,
        middleware=[
            Middleware(
                ThrottleMiddleware,
                middleware_throttles=[expensive_throttle, cheap_throttle],
                backend=inmemory_backend,
                sort="cheap_first",
            )
        ],
    )
    async with make_client(app1, base_url="http://0.0.0.0") as client:
        await client.get("/test")
    middleware1 = _find_throttle_middleware(app1)
    assert middleware1 is not None
    assert middleware1.middleware_throttles["http"] == [
        cheap_throttle,
        expensive_throttle,
    ]

    app2 = web_framework.build_app(
        http_routes=[HTTPRoute("/test", endpoint)],
        lifespan=inmemory_backend.lifespan,
        middleware=[
            Middleware(
                ThrottleMiddleware,
                middleware_throttles=[cheap_throttle, expensive_throttle],
                backend=inmemory_backend,
                sort="cheap_last",
            )
        ],
    )
    async with make_client(app2, base_url="http://0.0.0.0") as client:
        await client.get("/test")
    middleware2 = _find_throttle_middleware(app2)
    assert middleware2 is not None
    assert middleware2.middleware_throttles["http"] == [
        expensive_throttle,
        cheap_throttle,
    ]
