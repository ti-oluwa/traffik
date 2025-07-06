import asyncio
import os
import re

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from starlette.requests import HTTPConnection, Request

from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.throttles import HTTPThrottle

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, prefix="middleware-test", persistent=False)


async def _testclient_identifier(connection: HTTPConnection) -> str:
    return "testclient"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_initialization() -> None:
    """Test `MiddlewareThrottle` initialization with different parameters."""
    throttle = HTTPThrottle(
        uid="test-throttle",
        limit=5,
        seconds=60,
        identifier=_testclient_identifier,
    )

    # Test with string path
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/",
        methods={"GET", "POST"},
    )
    assert isinstance(middleware_throttle.path, re.Pattern)
    assert middleware_throttle.methods == frozenset(["get", "post"])
    assert middleware_throttle.hook is None

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
    assert middleware_throttle_all.hook is None


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_method_filtering(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that `MiddlewareThrottle` correctly filters by HTTP method."""
    async with inmemory_backend():
        throttle = HTTPThrottle(
            uid="method-filter-test",
            limit=1,
            seconds=60,
            identifier=_testclient_identifier,
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
        # GET request should create a throttling record
        initial_connections = (
            len(inmemory_backend.connection) if inmemory_backend.connection else 0
        )
        assert initial_connections == 1  # One connection created

        # POST request should be skipped (not throttled)
        result_post = await middleware_throttle(post_request)
        assert result_post is post_request  # Should return unchanged

        # POST request should not create a throttling record
        final_connections = (
            len(inmemory_backend.connection) if inmemory_backend.connection else 0
        )
        assert final_connections == initial_connections  # No new connection created


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_path_filtering(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that MiddlewareThrottle correctly filters by path pattern."""
    async with inmemory_backend():
        throttle = HTTPThrottle(
            uid="path-filter-test",
            limit=2,
            seconds=60,
            identifier=_testclient_identifier,
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
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_regex_path_filtering(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test MiddlewareThrottle with regex path patterns."""
    async with inmemory_backend():
        throttle = HTTPThrottle(
            uid="regex-path-test",
            limit=2,
            seconds=60,
            identifier=_testclient_identifier,
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

            # Track if throttle was actually applied by checking backend state
            initial_connections = (
                len(inmemory_backend.connection) if inmemory_backend.connection else 0
            )

            result = await middleware_throttle(request)
            assert result is request

            final_connections = (
                len(inmemory_backend.connection) if inmemory_backend.connection else 0
            )

            if should_match:
                # Throttle should have been applied, creating a record
                assert final_connections > initial_connections, (
                    f"Path {path} should have matched regex"
                )
            else:
                # Throttle should have been skipped
                assert final_connections == initial_connections, (
                    f"Path {path} should not have matched regex"
                )


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_hook_filtering(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test MiddlewareThrottle with custom hook filtering."""
    async with inmemory_backend():
        throttle = HTTPThrottle(
            uid="hook-filter-test",
            limit=2,
            seconds=60,
            identifier=_testclient_identifier,
        )

        # Hook that only applies to premium users
        async def premium_user_hook(connection: HTTPConnection) -> bool:
            return connection.scope.get("headers", {}).get("x-user-tier") == "premium"

        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            hook=premium_user_hook,
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

        # Track backend state
        initial_connections = (
            len(inmemory_backend.connection) if inmemory_backend.connection else 0
        )

        # Premium user should be throttled
        await middleware_throttle(premium_request)
        premium_connections = (
            len(inmemory_backend.connection) if inmemory_backend.connection else 0
        )
        assert premium_connections > initial_connections

        # Free user should be skipped
        await middleware_throttle(free_request)
        final_connections = (
            len(inmemory_backend.connection) if inmemory_backend.connection else 0
        )
        assert final_connections == premium_connections  # No change


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.middleware
@pytest.mark.fastapi
async def test_middleware_throttle_combined_filters(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test MiddlewareThrottle with multiple filters combined."""
    async with inmemory_backend():
        throttle = HTTPThrottle(
            uid="combined-filter-test",
            limit=2,
            seconds=60,
            identifier=_testclient_identifier,
        )

        async def auth_hook(connection: HTTPConnection) -> bool:
            headers = dict(connection.scope.get("headers", []))
            return "authorization" in headers

        # Combine method, path, and hook filters
        middleware_throttle = MiddlewareThrottle(
            throttle=throttle,
            path="/api/",
            methods={"POST"},
            hook=auth_hook,
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
            headers = [("authorization", "Bearer token")] if has_auth else []
            scope = {"type": "http", "method": method, "path": path, "headers": headers}
            request = Request(scope)

            initial_count = (
                len(inmemory_backend.connection) if inmemory_backend.connection else 0
            )
            await middleware_throttle(request)
            final_count = (
                len(inmemory_backend.connection) if inmemory_backend.connection else 0
            )

            if should_throttle:
                assert final_count > initial_count, (
                    f"{method} {path} auth={has_auth} should throttle"
                )
            else:
                assert final_count == initial_count, (
                    f"{method} {path} auth={has_auth} should not throttle"
                )


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_basic_functionality(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test basic ThrottleMiddleware functionality with FastAPI."""
    throttle = HTTPThrottle(
        uid="middleware-basic-test",
        limit=2,
        seconds=1,
        identifier=_testclient_identifier,
    )

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/",
        methods={"GET"},
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_multiple_throttles(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test ThrottleMiddleware with multiple MiddlewareThrottle instances."""
    # Different throttles for different endpoints
    api_throttle = HTTPThrottle(
        uid="api-throttle",
        limit=2,
        seconds=1,
        identifier=_testclient_identifier,
    )

    admin_throttle = HTTPThrottle(
        uid="admin-throttle",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    middleware_throttles = [
        MiddlewareThrottle(api_throttle, path="/api/", methods={"GET"}),
        MiddlewareThrottle(admin_throttle, path="/admin/", methods={"POST"}),
    ]

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_method_specificity(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that middleware only applies to specified HTTP methods."""
    throttle = HTTPThrottle(
        uid="method-specific-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    # Only throttle POST requests
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/data",
        methods={"POST"},
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_with_hook(inmemory_backend: InMemoryBackend) -> None:
    """Test ThrottleMiddleware with custom hook logic."""
    throttle = HTTPThrottle(
        uid="hook-middleware-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    # Only throttle requests with premium tier
    async def premium_only_hook(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-tier") == "premium"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        hook=premium_only_hook,
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_no_backend_specified(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test ThrottleMiddleware without explicit backend (should use lifespan backend)."""
    throttle = HTTPThrottle(
        uid="no-backend-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    # Don't specify backend - should use the one from lifespan
    app.add_middleware(
        ThrottleMiddleware,
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
@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.redis
@pytest.mark.fastapi
async def test_throttle_middleware_redis_backend(redis_backend: RedisBackend) -> None:
    """Test ThrottleMiddleware with Redis backend."""
    throttle = HTTPThrottle(
        uid="redis-middleware-test",
        limit=2,
        seconds=2,
        identifier=_testclient_identifier,
    )

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        path="/api/",
    )

    async with redis_backend():
        app = FastAPI()
        app.add_middleware(
            ThrottleMiddleware,
            middleware_throttles=[middleware_throttle],
            backend=redis_backend,
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
@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.concurrent
@pytest.mark.fastapi
async def test_throttle_middleware_concurrent_requests(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test ThrottleMiddleware under concurrent load."""
    throttle = HTTPThrottle(
        uid="concurrent-middleware-test",
        limit=3,
        seconds=5,
        identifier=_testclient_identifier,
    )

    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    async with inmemory_backend():
        app = FastAPI()
        app.add_middleware(
            ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_exemption_with_hook(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test middleware with exemption logic using hook."""
    throttle = HTTPThrottle(
        uid="exemption-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    # Exempt admin users from throttling
    async def non_admin_hook(connection: HTTPConnection) -> bool:
        headers = dict(connection.headers)
        return headers.get("x-user-role") != "admin"

    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        hook=non_admin_hook,
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_case_insensitive_methods(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that middleware handles HTTP methods in case-insensitive manner."""
    throttle = HTTPThrottle(
        uid="case-insensitive-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    # Specify methods in mixed case
    middleware_throttle = MiddlewareThrottle(
        throttle=throttle,
        methods={"GET", "post", "Put"},  # Mixed case
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_websocket_passthrough(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test that ThrottleMiddleware doesn't interfere with WebSocket connections."""
    throttle = HTTPThrottle(
        uid="websocket-test",
        limit=1,
        seconds=1,
        identifier=_testclient_identifier,
    )

    middleware_throttle = MiddlewareThrottle(throttle=throttle)

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_empty_throttles_list(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test ThrottleMiddleware with empty middleware_throttles list."""
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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


@pytest.mark.integration
@pytest.mark.middleware
@pytest.mark.fastapi
def test_throttle_middleware_multiple_overlapping_patterns(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Test ThrottleMiddleware with overlapping path patterns."""
    # Two throttles that could both match the same request
    general_throttle = HTTPThrottle(
        uid="general-throttle",
        limit=5,
        seconds=1,
        identifier=_testclient_identifier,
    )

    specific_throttle = HTTPThrottle(
        uid="specific-throttle",
        limit=2,
        seconds=1,
        identifier=_testclient_identifier,
    )

    middleware_throttles = [
        # More general pattern (processed first)
        MiddlewareThrottle(general_throttle, path="/api/"),
        # More specific pattern (processed second)
        MiddlewareThrottle(specific_throttle, path="/api/users/"),
    ]

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
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
