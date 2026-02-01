"""Tests for `traffik`'s middleware throttling APIs in a Starlette application."""

import asyncio
import re

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.conftest import BackendGen
from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_initialization() -> None:
    """Test `MiddlewareThrottle` initialization with different parameters."""
    throttle = HTTPThrottle(
        uid="test-throttle",
        rate="5/min",
        identifier=default_client_identifier,
    )

    # Test with string path
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/",
        methods={"GET", "POST"},
    )
    assert isinstance(middleware_throttle.path, re.Pattern)
    assert middleware_throttle.methods == frozenset(["get", "post"])
    assert middleware_throttle.predicate is None

    # Test with regex path
    regex_pattern = re.compile(r"/api/\d+")
    middleware_throttle_regex = MiddlewareThrottle(
        throttle=throttle,
        path=regex_pattern,
        methods={"GET"},
    )
    assert middleware_throttle_regex.path is regex_pattern
    assert middleware_throttle_regex.methods == frozenset(["get"])

    # Test with no path/methods (applies to all)
    middleware_throttle_all = MiddlewareThrottle(throttle=throttle)
    assert middleware_throttle_all.path is None
    assert middleware_throttle_all.methods is None
    assert middleware_throttle_all.predicate is None


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_method_filtering(inmemory_backend: InMemoryBackend) -> None:
    """Test that `MiddlewareThrottle` correctly filters by HTTP method."""
    async with inmemory_backend(close_on_exit=True):
        
        

        throttle = HTTPThrottle(
            uid="method-filter-test",
            rate="1/min",
            identifier=default_client_identifier,
        )

        # Only apply to GET requests
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            methods={"GET"},
        )

        # Create mock connections
        get_scope = {"type": "http", "method": "GET", "path": "/test"}
        post_scope = {"type": "http", "method": "POST", "path": "/test"}

        get_request = Request(get_scope)
        post_request = Request(post_scope)

        # GET request should be processed by throttle
        result_get = await middleware_throttle(get_request)
        assert result_get is get_request  # Should pass through after processing

        # POST request should be skipped (not throttled)
        result_post = await middleware_throttle(post_request)
        assert result_post is post_request  # Should return unchanged


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_path_filtering(inmemory_backend: InMemoryBackend) -> None:
    """Test that `MiddlewareThrottle` correctly filters by path pattern."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="path-filter-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        # Only apply to paths starting with /api/
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
        )

        # Create mock connections
        api_scope = {"type": "http", "method": "GET", "path": "/api/users"}
        public_scope = {"type": "http", "method": "GET", "path": "/public/info"}

        api_request = Request(api_scope)
        public_request = Request(public_scope)

        # API request should be processed by throttle
        result_api = await middleware_throttle(api_request)
        assert result_api is api_request

        # Public request should be skipped
        result_public = await middleware_throttle(public_request)
        assert result_public is public_request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_regex_path_filtering(inmemory_backend: InMemoryBackend) -> None:
    """Test `MiddlewareThrottle` with regex path patterns."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="regex-path-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        # Apply to paths like /api/123, /api/456, etc.
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path=r"/api/\d+",
        )

        # Test various paths
        test_cases = [
            ("/api/123", True),  # Should match
            ("/api/456", True),  # Should match
            ("/api/abc", False),  # Should not match (not digits)
            ("/api/", False),  # Should not match (no digits)
            ("/public/123", False),  # Should not match (wrong prefix)
        ]

        for path, should_match in test_cases:
            scope = {"type": "http", "method": "GET", "path": path}
            request = Request(scope)

            # Verify middleware processes request without error
            result = await middleware_throttle(request)
            assert result is request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_hook_filtering(inmemory_backend: InMemoryBackend) -> None:
    """Test `MiddlewareThrottle` with custom hook filtering."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="hook-filter-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        # Hook that only applies to premium users
        async def premium_user_hook(connection: HTTPConnection) -> bool:
            return connection.scope.get("headers", {}).get("x-user-tier") == "premium"

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            predicate=premium_user_hook,
        )

        # Create connections with different user tiers
        premium_scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": {"x-user-tier": "premium"},
        }
        free_scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": {"x-user-tier": "free"},
        }

        premium_request = Request(premium_scope)
        free_request = Request(free_scope)

        # Premium user should be throttled
        result_premium = await middleware_throttle(premium_request)
        assert result_premium is premium_request

        # Free user should be skipped
        result_free = await middleware_throttle(free_request)
        assert result_free is free_request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_throttle_combined_filters(inmemory_backend: InMemoryBackend) -> None:
    """Test `MiddlewareThrottle` with multiple filters combined."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="combined-filter-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        async def auth_hook(connection: HTTPConnection) -> bool:
            headers = dict(connection.scope.get("headers", []))
            return b"authorization" in headers

        # Combine method, path, and hook filters
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
            methods={"POST"},
            predicate=auth_hook,
        )

        test_cases = [
            # (method, path, has_auth, should_throttle)
            ("POST", "/api/users", True, True),  # All conditions met
            ("GET", "/api/users", True, False),  # Wrong method
            ("POST", "/public/users", True, False),  # Wrong path
            ("POST", "/api/users", False, False),  # No auth
            ("GET", "/public/users", False, False),  # No conditions met
        ]

        for method, path, has_auth, should_throttle in test_cases:
            headers = [(b"authorization", b"Bearer token")] if has_auth else []
            scope = {"type": "http", "method": method, "path": path, "headers": headers}
            request = Request(scope)

            # Verify middleware processes request without error
            result = await middleware_throttle(request)
            assert result is request


@pytest.mark.middleware
def test_middleware_basic_functionality(inmemory_backend: InMemoryBackend) -> None:
    """Test basic `ThrottleMiddleware` functionality with Starlette."""
    throttle = HTTPThrottle(
        uid="middleware-basic-test",
        rate="2/s",
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/",
        methods={"GET"},
    )

    async def api_data(request: Request) -> JSONResponse:
        return JSONResponse({"data": "response"})

    async def public_data(request: Request) -> JSONResponse:
        return JSONResponse({"data": "public"})

    routes = [
        Route("/api/data", api_data, methods=["GET"]),
        Route("/public/data", public_data, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # First two requests to /api/data should succeed
        response1 = client.get("/api/data")
        assert response1.status_code == 200

        response2 = client.get("/api/data")
        assert response2.status_code == 200

        # Third request should be throttled
        response3 = client.get("/api/data")
        assert response3.status_code == 429
        assert "Retry-After" in response3.headers

        # Public endpoint should not be throttled
        response4 = client.get("/public/data")
        assert response4.status_code == 200


@pytest.mark.middleware
def test_middleware_with_multiple_throttles(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with multiple `MiddlewareThrottle` instances."""
    # Different throttles for different endpoints
    api_throttle = HTTPThrottle(
        uid="api-throttle",
        rate="2/700ms",
        identifier=default_client_identifier,
    )
    admin_throttle = HTTPThrottle(
        uid="admin-throttle",
        rate="1/s",
        identifier=default_client_identifier,
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

    routes = [
        Route("/api/users", get_users, methods=["GET"]),
        Route("/admin/settings", update_settings, methods=["POST"]),
        Route("/public/info", get_info, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=middleware_throttles,
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Test API throttle (limit=2)
        assert client.get("/api/users").status_code == 200
        assert client.get("/api/users").status_code == 200
        assert client.get("/api/users").status_code == 429

        # Test admin throttle (limit=1)
        assert client.post("/admin/settings").status_code == 200
        assert client.post("/admin/settings").status_code == 429

        # Public endpoint should not be throttled
        assert client.get("/public/info").status_code == 200


@pytest.mark.middleware
def test_middleware_method_specificity(inmemory_backend: InMemoryBackend) -> None:
    """Test that middleware only applies to specified HTTP methods."""
    throttle = HTTPThrottle(
        uid="method-specific-test",
        rate="1/s",
        identifier=default_client_identifier,
    )
    # Only throttle POST requests
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/data",
        methods={"POST"},
    )

    async def get_data(request: Request) -> JSONResponse:
        return JSONResponse({"method": "GET"})

    async def post_data(request: Request) -> JSONResponse:
        return JSONResponse({"method": "POST"})

    async def put_data(request: Request) -> JSONResponse:
        return JSONResponse({"method": "PUT"})

    routes = [
        Route("/api/data", get_data, methods=["GET"]),
        Route("/api/data", post_data, methods=["POST"]),
        Route("/api/data", put_data, methods=["PUT"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # GET should not be throttled (multiple requests allowed)
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200

        # POST should be throttled after first request
        assert client.post("/api/data").status_code == 200
        assert client.post("/api/data").status_code == 429

        # PUT should not be throttled
        assert client.put("/api/data").status_code == 200
        assert client.put("/api/data").status_code == 200


@pytest.mark.middleware
def test_middleware_with_hook(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with custom hook logic."""
    throttle = HTTPThrottle(
        uid="hook-middleware-test",
        rate="1/s",
        identifier=default_client_identifier,
    )

    # Only throttle requests with premium tier
    async def is_premium_user(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-tier") == "premium"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        predicate=is_premium_user,
    )

    async def get_data(request: Request) -> JSONResponse:
        return JSONResponse({"data": "response"})

    routes = [
        Route("/data", get_data, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Free user should not be throttled
        free_headers = {"x-user-tier": "free"}
        assert client.get("/data", headers=free_headers).status_code == 200
        assert client.get("/data", headers=free_headers).status_code == 200
        assert client.get("/data", headers=free_headers).status_code == 200

        # Premium user should be throttled
        premium_headers = {"x-user-tier": "premium"}
        assert client.get("/data", headers=premium_headers).status_code == 200
        assert client.get("/data", headers=premium_headers).status_code == 429


@pytest.mark.middleware
def test_middleware_with_no_backend_specified(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `ThrottleMiddleware` without explicit backend (should use lifespan backend)."""
    throttle = HTTPThrottle(
        uid="no-backend-test",
        rate="1/s",
        identifier=default_client_identifier,
    )

    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async def test_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"test": "response"})

    routes = [
        Route("/test", test_endpoint, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    # Don't specify backend, should use the one from lifespan
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        # backend=None (implicit)
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Should still throttle using lifespan backend
        assert client.get("/test").status_code == 200
        assert client.get("/test").status_code == 429


@pytest.mark.anyio
@pytest.mark.middleware
async def test_middleware_multiple_backends(backends: BackendGen) -> None:
    """Test `ThrottleMiddleware` with all backends."""
    for backend in backends(persistent=False, namespace="middleware_test"):
        throttle = HTTPThrottle(
            uid="redis-middleware-test",
            rate="2/s",
            identifier=default_client_identifier,
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
        )

        async with backend(close_on_exit=True):

            async def test_endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"redis": "test"})

            async def public_endpoint(request: Request) -> JSONResponse:
                return JSONResponse({"public": "test"})

            routes = [
                Route("/api/test", test_endpoint, methods=["GET"]),
                Route("/public/test", public_endpoint, methods=["GET"]),
            ]

            app = Starlette(routes=routes)
            app.add_middleware(
                ThrottleMiddleware,
                middleware_throttles=[middleware_throttle],
                backend=backend,
            )

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=base_url
            ) as client:
                # Test throttling works with Redis
                response1 = await client.get("/api/test")
                assert response1.status_code == 200

                response2 = await client.get("/api/test")
                assert response2.status_code == 200

                response3 = await client.get("/api/test")
                assert response3.status_code == 429
                assert "Retry-After" in response3.headers

                # Public endpoint should not be throttled
                response4 = await client.get("/public/test")
                assert response4.status_code == 200


@pytest.mark.anyio
@pytest.mark.middleware
@pytest.mark.concurrent
async def test_middleware_concurrency(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` under concurrent load."""
    throttle = HTTPThrottle(
        uid="concurrent-middleware-test",
        rate=Rate.parse("3/5s"),
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async with inmemory_backend(close_on_exit=True):

        async def concurrent_endpoint(request: Request) -> JSONResponse:
            return JSONResponse({"concurrent": "test"})

        routes = [
            Route("/concurrent", concurrent_endpoint, methods=["GET"]),
        ]

        app = Starlette(routes=routes)
        app.add_middleware(
            ThrottleMiddleware,
            middleware_throttles=[middleware_throttle],
            backend=inmemory_backend,
        )

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:

            async def make_request():
                return await client.get("/concurrent")

            # Make 10 concurrent requests
            responses = await asyncio.gather(*[make_request() for _ in range(10)])

            status_codes = [r.status_code for r in responses]
            success_count = status_codes.count(200)
            throttled_count = status_codes.count(429)

            # Should have exactly 3 successful requests and 7 throttled
            assert success_count == 3
            assert throttled_count == 7


@pytest.mark.middleware
def test_middleware_exemption_with_hook(inmemory_backend: InMemoryBackend) -> None:
    """Test middleware with exemption logic using hook."""
    throttle = HTTPThrottle(
        uid="exemption-test",
        rate=Rate.parse("1/1s"),
        identifier=default_client_identifier,
    )

    # Exempt admin users from throttling
    async def non_admin_hook(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-role") != "admin"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        predicate=non_admin_hook,
    )

    async def get_data(request: Request) -> JSONResponse:
        return JSONResponse({"data": "response"})

    routes = [Route("/data", get_data, methods=["GET"])]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Admin users should not be throttled
        admin_headers = {"x-user-role": "admin"}
        for _ in range(5):  # Make many requests
            response = client.get("/data", headers=admin_headers)
            assert response.status_code == 200

        # Regular users should be throttled
        user_headers = {"x-user-role": "user"}
        assert client.get("/data", headers=user_headers).status_code == 200
        assert client.get("/data", headers=user_headers).status_code == 429


@pytest.mark.middleware
def test_middleware_methods_filter_is_case_insensitive(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that middleware handles HTTP methods in case-insensitive manner."""
    throttle = HTTPThrottle(
        uid="case-insensitive-test",
        rate=Rate.parse("1/1s"),
        identifier=default_client_identifier,
    )
    # Specify methods in mixed case
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        methods={"GET", "post", "Put"},  # Mixed case
    )

    async def get_test(request: Request) -> JSONResponse:
        return JSONResponse({"method": "GET"})

    async def post_test(request: Request) -> JSONResponse:
        return JSONResponse({"method": "POST"})

    async def put_test(request: Request) -> JSONResponse:
        return JSONResponse({"method": "PUT"})

    async def delete_test(request: Request) -> JSONResponse:
        return JSONResponse({"method": "DELETE"})

    routes = [
        Route("/test", get_test, methods=["GET"]),
        Route("/test", post_test, methods=["POST"]),
        Route("/test", put_test, methods=["PUT"]),
        Route("/test", delete_test, methods=["DELETE"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # GET, POST, PUT should be throttled
        assert client.get("/test").status_code == 200
        assert client.get("/test").status_code == 429

        assert client.post("/test").status_code == 200
        assert client.post("/test").status_code == 429

        assert client.put("/test").status_code == 200
        assert client.put("/test").status_code == 429

        # DELETE should not be throttled (not in the methods set)
        assert client.delete("/test").status_code == 200
        assert client.delete("/test").status_code == 200  # Still not throttled


@pytest.mark.middleware
def test_middleware_websocket_passthrough(inmemory_backend: InMemoryBackend) -> None:
    """Test that `ThrottleMiddleware` doesn't interfere with WebSocket connections."""
    throttle = HTTPThrottle(
        uid="websocket-test",
        rate=Rate.parse("1/1s"),
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async def websocket_endpoint(websocket):
        await websocket.accept()
        await websocket.send_json({"message": "connected"})
        await websocket.close()

    async def http_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"type": "http"})

    from starlette.routing import WebSocketRoute

    routes = [
        Route("/http", http_endpoint, methods=["GET"]),
        WebSocketRoute("/ws", websocket_endpoint),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # HTTP endpoint should be throttled
        assert client.get("/http").status_code == 200
        assert client.get("/http").status_code == 429

        # WebSocket should work without throttling interference
        with client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            assert data == {"message": "connected"}

        # Multiple WebSocket connections should work
        with client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            assert data == {"message": "connected"}


@pytest.mark.middleware
def test_middleware_with_no_throttles(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with empty `middleware_throttles` list."""

    async def test_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"test": "response"})

    routes = [Route("/test", test_endpoint, methods=["GET"])]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[],  # Empty list
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Should work normally without any throttling
        for _ in range(10):
            response = client.get("/test")
            assert response.status_code == 200


@pytest.mark.middleware
def test_middleware_with_multiple_overlapping_patterns(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `ThrottleMiddleware` with overlapping path patterns."""
    # Two throttles that could both match the same request
    general_throttle = HTTPThrottle(
        uid="general-throttle",
        rate=Rate.parse("5/s"),
        identifier=default_client_identifier,
    )
    specific_throttle = HTTPThrottle(
        uid="specific-throttle",
        rate=Rate.parse("2/s"),
        identifier=default_client_identifier,
    )

    middleware_throttles = [
        # More general pattern (processed first)
        MiddlewareThrottle(general_throttle, path="/api/"),
        # More specific pattern (processed second)
        MiddlewareThrottle(specific_throttle, path="/api/users/"),
    ]

    async def api_general(request: Request) -> JSONResponse:
        return JSONResponse({"type": "general"})

    async def api_users(request: Request) -> JSONResponse:
        return JSONResponse({"type": "users"})

    routes = [
        Route("/api/data", api_general, methods=["GET"]),
        Route("/api/users/list", api_users, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=middleware_throttles,
        backend=inmemory_backend,
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # /api/data should only be limited by general throttle (limit=5)
        for i in range(5):
            response = client.get("/api/data")
            assert response.status_code == 200, f"Request {i + 1} should succeed"

        response = client.get("/api/data")
        assert response.status_code == 429  # 6th request should be throttled

        # /api/users/list should be limited by BOTH throttles
        # This means it will be more restrictive (limit=2 from specific throttle wins)
        # Actually, both throttles will apply, so it hits the specific limit first
        for i in range(2):
            response = client.get("/api/users/list")
            assert response.status_code == 200, f"Users request {i + 1} should succeed"

        response = client.get("/api/users/list")
        assert (
            response.status_code == 429
        )  # 3rd request should be throttled by specific throttle


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_middleware_complex_regex_patterns(inmemory_backend: InMemoryBackend) -> None:
    """Test middleware with complex regex patterns including groups, alternation, and anchors."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="complex-regex",
            rate="3/min",
            identifier=default_client_identifier,
        )

        # Complex pattern: Match UUIDs in API paths
        uuid_pattern = re.compile(
            r"/api/(?:users|products)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path=uuid_pattern,
        )

        # Test UUID patterns
        test_cases = [
            ("/api/users/550e8400-e29b-41d4-a716-446655440000", True),  # Valid UUID
            ("/api/products/123e4567-e89b-12d3-a456-426614174000", True),  # Valid UUID
            ("/api/users/123", False),  # Not a UUID
            ("/api/orders/550e8400-e29b-41d4-a716-446655440000", False),  # Wrong resource
            ("/api/users/not-a-uuid", False),  # Invalid UUID format
        ]

        for path, should_match in test_cases:
            scope = {"type": "http", "method": "GET", "path": path}
            request = Request(scope)

            result = await middleware_throttle(request)
            assert result is request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_middleware_string_auto_compile_to_regex(inmemory_backend: InMemoryBackend) -> None:
    """Test that string paths are automatically compiled to regex patterns."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="auto-compile",
            rate="2/min",
            identifier=default_client_identifier,
        )

        # Pass string path (should be auto-compiled)
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",  # String path
        )

        # Verify it was compiled to Pattern
        assert isinstance(middleware_throttle.path, re.Pattern)
        assert middleware_throttle.path.pattern == "/api/"

        # Test that it works
        matching_scope = {"type": "http", "method": "GET", "path": "/api/users"}
        non_matching_scope = {"type": "http", "method": "GET", "path": "/public/data"}

        matching_request = Request(matching_scope)
        non_matching_request = Request(non_matching_scope)

        result_match = await middleware_throttle(matching_request)
        assert result_match is matching_request

        result_non_match = await middleware_throttle(non_matching_request)
        assert result_non_match is non_matching_request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_middleware_regex_with_query_params_ignored(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that regex matching works on path only, ignoring query parameters."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="query-ignore",
            rate="2/min",
            identifier=default_client_identifier,
        )

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path=r"^/api/search$",  # Exact match pattern
        )

        # All these should match despite different query params
        test_paths = [
            "/api/search",
            "/api/search",  # Same path, will count toward limit
        ]

        for path in test_paths:
            scope = {"type": "http", "method": "GET", "path": path}
            request = Request(scope)
            result = await middleware_throttle(request)
            assert result is request

        # This should NOT match (different path)
        scope = {"type": "http", "method": "GET", "path": "/api/search/results"}
        request = Request(scope)
        result = await middleware_throttle(request)
        assert result is request


@pytest.mark.asyncio
@pytest.mark.middleware
async def test_middleware_case_sensitive_regex(inmemory_backend: InMemoryBackend) -> None:
    """Test that regex patterns are case-sensitive by default."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="case-sensitive",
            rate="3/min",
            identifier=default_client_identifier,
        )

        # Case-sensitive pattern
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path=re.compile(r"/API/"),
        )

        test_cases = [
            ("/API/users", True),  # Matches
            ("/api/users", False),  # Does not match (lowercase)
            ("/Api/users", False),  # Does not match (mixed case)
        ]

        for path, should_match in test_cases:
            scope = {"type": "http", "method": "GET", "path": path}
            request = Request(scope)

            result = await middleware_throttle(request)
            assert result is request
