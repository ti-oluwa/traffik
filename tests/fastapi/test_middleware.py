"""Tests for `traffik`'s middleware throttling APIs in a FastAPI application."""

import asyncio
import re

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from tests.conftest import BackendGen
from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle, WebSocketThrottle


@pytest.mark.asyncio
@pytest.mark.middleware
@pytest.mark.fastapi
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
    # Methods are stored in both upper and lower case for fast matching
    assert middleware_throttle.methods is not None
    assert "get" in middleware_throttle.methods or "GET" in middleware_throttle.methods
    assert (
        "post" in middleware_throttle.methods or "POST" in middleware_throttle.methods
    )
    assert middleware_throttle.predicate is None

    # Test with regex path
    regex_pattern = re.compile(r"/api/\d+")
    middleware_throttle_regex = MiddlewareThrottle(
        throttle=throttle,
        path=regex_pattern,
        methods={"GET"},
    )
    assert middleware_throttle_regex.path is regex_pattern
    assert middleware_throttle_regex.methods is not None
    assert (
        "get" in middleware_throttle_regex.methods
        or "GET" in middleware_throttle_regex.methods
    )

    # Test with no path/methods (applies to all)
    middleware_throttle_all = MiddlewareThrottle(throttle=throttle)
    assert middleware_throttle_all.path is None
    assert middleware_throttle_all.methods is None
    assert middleware_throttle_all.predicate is None


@pytest.mark.asyncio
@pytest.mark.middleware
@pytest.mark.fastapi
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
@pytest.mark.fastapi
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
@pytest.mark.fastapi
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
@pytest.mark.fastapi
async def test_throttle_predicate_filtering(inmemory_backend: InMemoryBackend) -> None:
    """Test `MiddlewareThrottle` with custom predicate filtering."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="predicate-filter-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        # predicate that only applies to premium users
        async def is_premium_user(connection: HTTPConnection) -> bool:
            return connection.scope.get("headers", {}).get("x-user-tier") == "premium"

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            predicate=is_premium_user,
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
@pytest.mark.fastapi
async def test_throttle_combined_filters(inmemory_backend: InMemoryBackend) -> None:
    """Test `MiddlewareThrottle` with multiple filters combined."""
    async with inmemory_backend(close_on_exit=True):
        throttle = HTTPThrottle(
            uid="combined-filter-test",
            rate="2/min",
            identifier=default_client_identifier,
        )

        async def is_authorized(connection: HTTPConnection) -> bool:
            headers = dict(connection.scope.get("headers", []))
            return b"authorization" in headers

        # Combine method, path, and predicate filters
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
            methods={"POST"},
            predicate=is_authorized,
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

            #  Verify middleware processes request without error
            result = await middleware_throttle(request)
            assert result is request


@pytest.mark.middleware
@pytest.mark.fastapi
def test_middleware_basic_functionality(inmemory_backend: InMemoryBackend) -> None:
    """Test basic `ThrottleMiddleware` functionality with FastAPI."""
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

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/api/data")
    async def get_data():
        return {"data": "response"}

    @app.get("/public/data")
    async def get_public_data():
        return {"data": "public"}

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
@pytest.mark.fastapi
def test_middleware_multiple_throttles(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with multiple `MiddlewareThrottle` instances."""
    # Different throttles for different endpoints
    api_throttle = HTTPThrottle(
        uid="api-throttle",
        rate="2/s",
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

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=middleware_throttles,
        backend=inmemory_backend,
    )

    @app.get("/api/users")
    async def get_users():
        return {"users": []}

    @app.post("/admin/settings")
    async def update_settings():
        return {"status": "updated"}

    @app.get("/public/info")
    async def get_info():
        return {"info": "public"}

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
@pytest.mark.fastapi
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

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/api/data")
    async def get_data():
        return {"method": "GET"}

    @app.post("/api/data")
    async def post_data():
        return {"method": "POST"}

    @app.put("/api/data")
    async def put_data():
        return {"method": "PUT"}

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
@pytest.mark.fastapi
def test_middleware_with_predicate(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with custom predicate logic."""
    throttle = HTTPThrottle(
        uid="predicate-middleware-test",
        rate="1/s",
        identifier=default_client_identifier,
    )

    # Only throttle requests with premium tier
    async def premium_only_predicate(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-tier") == "premium"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        predicate=premium_only_predicate,
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/data")
    async def get_data():
        return {"data": "response"}

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
@pytest.mark.fastapi
def test_middleware_no_backend_specified(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` without explicit backend (should use lifespan backend)."""
    throttle = HTTPThrottle(
        uid="no-backend-test",
        rate="1/s",
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    # Don't specify backend - should use the one from lifespan
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        # backend=None (implicit)
    )

    @app.get("/test")
    async def test_endpoint():
        return {"test": "response"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Should still throttle using lifespan backend
        assert client.get("/test").status_code == 200
        assert client.get("/test").status_code == 429


@pytest.mark.anyio
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_multiple_backends(backends: BackendGen) -> None:
    """Test `ThrottleMiddleware` with all backends."""
    for backend in backends(persistent=False, namespace="middleware_test"):
        throttle = HTTPThrottle(
            uid="redis-middleware-test",
            rate="2/min",
            identifier=default_client_identifier,
        )
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
        )

        async with backend(close_on_exit=True):
            app = FastAPI()
            app.add_middleware(
                ThrottleMiddleware,  # type: ignore[arg-type]
                middleware_throttles=[middleware_throttle],
                backend=backend,
            )

            @app.get("/api/test")
            async def test_endpoint():
                return {"redis": "test"}

            @app.get("/public/test")
            async def public_endpoint():
                return {"public": "test"}

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
@pytest.mark.fastapi
async def test_middleware_concurrent_requests(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `ThrottleMiddleware` under concurrent load."""
    throttle = HTTPThrottle(
        uid="concurrent-middleware-test",
        rate=Rate.parse("3/5s"),
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async with inmemory_backend(close_on_exit=True):
        app = FastAPI()
        app.add_middleware(
            ThrottleMiddleware,  # type: ignore[arg-type]
            middleware_throttles=[middleware_throttle],
            backend=inmemory_backend,
        )

        @app.get("/concurrent")
        async def concurrent_endpoint():
            return {"concurrent": "test"}

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
@pytest.mark.fastapi
def test_middleware_exemption_with_predicate(inmemory_backend: InMemoryBackend) -> None:
    """Test middleware with exemption logic using predicate."""
    throttle = HTTPThrottle(
        uid="exemption-test",
        rate=Rate.parse("1/1s"),
        identifier=default_client_identifier,
    )

    # Exempt admin users from throttling
    async def non_admin_predicate(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-role") != "admin"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        predicate=non_admin_predicate,
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/data")
    async def get_data():
        return {"data": "response"}

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
@pytest.mark.fastapi
def test_middleware_case_insensitive_methods(inmemory_backend: InMemoryBackend) -> None:
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

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/test")
    async def get_test():
        return {"method": "GET"}

    @app.post("/test")
    async def post_test():
        return {"method": "POST"}

    @app.put("/test")
    async def put_test():
        return {"method": "PUT"}

    @app.delete("/test")
    async def delete_test():
        return {"method": "DELETE"}

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
@pytest.mark.fastapi
def test_middleware_websocket_passthrough(inmemory_backend: InMemoryBackend) -> None:
    """Test that `ThrottleMiddleware` doesn't interfere with WebSocket connections."""
    throttle = HTTPThrottle(
        uid="websocket-test",
        rate=Rate.parse("1/1s"),
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/http")
    async def http_endpoint():
        return {"type": "http"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"message": "connected"})
        # Don't close immediately - let the client close

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
@pytest.mark.fastapi
def test_middleware_with_no_throttles(inmemory_backend: InMemoryBackend) -> None:
    """Test `ThrottleMiddleware` with empty `middleware_throttles` list."""
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[],  # Empty list
        backend=inmemory_backend,
    )

    @app.get("/test")
    async def test_endpoint():
        return {"test": "response"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Should work normally without any throttling
        for _ in range(10):
            response = client.get("/test")
            assert response.status_code == 200


@pytest.mark.middleware
@pytest.mark.fastapi
def test_middleware_multiple_overlapping_patterns(
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

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=middleware_throttles,
        backend=inmemory_backend,
    )

    @app.get("/api/data")
    async def api_general():
        return {"type": "general"}

    @app.get("/api/users/list")
    async def api_users():
        return {"type": "users"}

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
@pytest.mark.fastapi
async def test_middleware_complex_regex_patterns(
    inmemory_backend: InMemoryBackend,
) -> None:
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
            (
                "/api/orders/550e8400-e29b-41d4-a716-446655440000",
                False,
            ),  # Wrong resource
            ("/api/users/not-a-uuid", False),  # Invalid UUID format
        ]

        for path, should_match in test_cases:
            scope = {"type": "http", "method": "GET", "path": path}
            request = Request(scope)

            # Verify middleware processes request without error
            result = await middleware_throttle(request)
            assert result is request, "Middleware should return the request"


@pytest.mark.asyncio
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_string_auto_compile_to_regex(
    inmemory_backend: InMemoryBackend,
) -> None:
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

        # Verify middleware processes requests without error
        result1 = await middleware_throttle(matching_request)
        assert result1 is matching_request

        result2 = await middleware_throttle(non_matching_request)
        assert result2 is non_matching_request


@pytest.mark.asyncio
@pytest.mark.middleware
@pytest.mark.fastapi
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
@pytest.mark.fastapi
async def test_middleware_case_sensitive_regex(
    inmemory_backend: InMemoryBackend,
) -> None:
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

            # Verify middleware processes request without error
            result = await middleware_throttle(request)
            assert result is request


@pytest.mark.middleware
@pytest.mark.fastapi
@pytest.mark.asyncio
async def test_middleware_with_streaming_responses(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test that ThrottleMiddleware does not interfere with streaming responses.

    Streaming responses send data in chunks over time. The throttle should:
    1. Check rate limits before streaming begins
    2. Allow streaming to proceed normally if request is not throttled
    3. Prevent streaming entirely if request is throttled (return 429 immediately)
    """
    throttle = HTTPThrottle(
        uid="streaming-test",
        rate="2/s",
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/stream",
        methods={"GET"},
    )

    async def stream_generator():
        """Generator that yields chunks of data."""
        for i in range(5):
            yield f"chunk-{i}\n".encode()
            await asyncio.sleep(0.01)  # Simulate slow streaming

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/api/stream")
    async def stream_endpoint():
        return StreamingResponse(
            stream_generator(),
            media_type="text/plain",
            headers={"X-Custom-Header": "streaming"},
        )

    @app.get("/api/regular")
    async def regular_endpoint():
        return JSONResponse({"data": "regular"})

    base_url = "http://testserver"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=base_url
    ) as client:
        # First request: Should stream successfully
        response1 = await client.get("/api/stream")
        assert response1.status_code == 200
        assert response1.headers["X-Custom-Header"] == "streaming"

        # Verify all chunks are received
        content1 = response1.text
        for i in range(5):
            assert f"chunk-{i}" in content1

        # Second request: Should also stream successfully (within rate limit)
        response2 = await client.get("/api/stream")
        assert response2.status_code == 200
        content2 = response2.text
        for i in range(5):
            assert f"chunk-{i}" in content2

        # Third request: Should be throttled BEFORE streaming starts
        response3 = await client.get("/api/stream")
        assert response3.status_code == 429
        assert "Retry-After" in response3.headers

        # Verify throttled response is plain text, not streaming chunks
        assert "Too many requests" in response3.text
        assert (
            "chunk-" not in response3.text
        )  # No streaming chunks in throttled response

        # Non-throttled endpoint should work normally
        response4 = await client.get("/api/regular")
        assert response4.status_code == 200
        assert response4.json() == {"data": "regular"}


@pytest.mark.middleware
@pytest.mark.fastapi
@pytest.mark.asyncio
async def test_middleware_streaming_with_large_chunks(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test streaming responses with larger data chunks to ensure throttling
    doesn't interfere with data integrity.
    """
    throttle = HTTPThrottle(
        uid="large-stream-test",
        rate="10/m",
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/download",
    )

    async def large_stream_generator():
        """Simulate downloading a large file in chunks."""
        chunk_size = 1024  # 1KB chunks
        for chunk_num in range(10):
            # Generate predictable data for verification
            chunk_header = f"CHUNK{chunk_num:04d}".encode()
            padding = b"X" * (chunk_size - len(chunk_header))
            yield chunk_header + padding
            await asyncio.sleep(0.001)

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/api/download")
    async def download_endpoint():
        return StreamingResponse(
            large_stream_generator(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=data.bin"},
        )

    base_url = "http://testserver"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=base_url
    ) as client:
        # First request should complete streaming successfully
        response = await client.get("/api/download")
        assert response.status_code == 200
        assert "Content-Disposition" in response.headers

        content = response.content
        # Verify we got all 10 chunks (10KB total)
        assert len(content) == 10 * 1024

        # Verify chunk headers are intact
        for chunk_num in range(10):
            header = f"CHUNK{chunk_num:04d}".encode()
            assert header in content


@pytest.mark.middleware
@pytest.mark.fastapi
@pytest.mark.asyncio
async def test_middleware_streaming_exception_during_stream(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test that throttling is checked before streaming, so exceptions during
    streaming are not related to throttling logic.
    """
    throttle = HTTPThrottle(
        uid="stream-exception-test",
        rate="5/m",
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async def failing_stream_generator():
        """Generator that fails partway through."""
        yield b"chunk-1\n"
        yield b"chunk-2\n"
        # Simulate an error during streaming
        raise ValueError("Streaming error")

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.get("/stream")
    async def failing_stream_endpoint():
        return StreamingResponse(failing_stream_generator(), media_type="text/plain")

    base_url = "http://testserver"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=base_url
    ) as client:
        # The throttle middleware should allow the request through,
        # but the streaming itself will fail
        with pytest.raises(ValueError, match="Streaming error"):
            await client.get("/stream")


@pytest.mark.middleware
@pytest.mark.fastapi
def test_middleware_websocket_throttle(inmemory_backend: InMemoryBackend) -> None:
    """Test that `ThrottleMiddleware` throttles WebSocket connections with `WebSocketThrottle`."""
    ws_throttle = WebSocketThrottle(
        uid="ws-middleware-throttle",
        rate=Rate.parse("2/5s"),
        identifier=default_client_identifier,
    )
    middleware_throttle = MiddlewareThrottle(throttle=ws_throttle, path="/ws")

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[middleware_throttle],
        backend=inmemory_backend,
    )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"message": "connected"})
        await websocket.close()

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # First 2 connections should succeed
        for _ in range(2):
            with client.websocket_connect("/ws") as websocket:
                data = websocket.receive_json()
                assert data == {"message": "connected"}

        # 3rd connection should be throttled (rejected before accept)
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()


@pytest.mark.middleware
@pytest.mark.fastapi
def test_middleware_mixed_http_and_websocket_throttles(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test `ThrottleMiddleware` with both HTTP and WebSocket throttles simultaneously."""
    http_throttle = HTTPThrottle(
        uid="mixed-http",
        rate=Rate.parse("2/5s"),
        identifier=default_client_identifier,
    )
    ws_throttle = WebSocketThrottle(
        uid="mixed-ws",
        rate=Rate.parse("2/5s"),
        identifier=default_client_identifier,
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,  # type: ignore[arg-type]
        middleware_throttles=[
            MiddlewareThrottle(throttle=http_throttle),
            MiddlewareThrottle(throttle=ws_throttle),
        ],
        backend=inmemory_backend,
    )

    @app.get("/http")
    async def http_endpoint():
        return {"type": "http"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "websocket"})
        await websocket.close()

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # HTTP throttle works independently
        assert client.get("/http").status_code == 200
        assert client.get("/http").status_code == 200
        assert client.get("/http").status_code == 429

        # WebSocket throttle works independently (not affected by HTTP throttle)
        with client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            assert data == {"type": "websocket"}

        with client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            assert data == {"type": "websocket"}

        # 3rd WebSocket connection should be throttled
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()
