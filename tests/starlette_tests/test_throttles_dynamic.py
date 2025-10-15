import os

import pytest
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, namespace="redis-test", persistent=False)


@pytest.mark.asyncio
@pytest.mark.throttle
@pytest.mark.native
async def test_throttle_dynamic_backend(
    inmemory_backend: InMemoryBackend,
) -> None:
    # This throttle should raise an error if dynamic_backend is True
    # and a backend is provided
    with pytest.raises(ValueError):
        throttle = HTTPThrottle(
            "test-dynamic-backend-with-backend",
            rate=Rate(2, seconds=50, minutes=2, hours=1),
            dynamic_backend=True,
            backend=inmemory_backend,
        )

    # This throttle should respect the context of the backend
    # and should use the backend from the context if available.
    throttle = HTTPThrottle(
        "test-dynamic-backend-no-backend",
        rate=Rate(2, seconds=50, minutes=2, hours=1),
        dynamic_backend=True,
        identifier=default_client_identifier,
    )
    assert throttle.dynamic_backend is True
    assert throttle.backend is None

    dummy_request = Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
            "app": None,
        }
    )

    async with inmemory_backend():
        # Use the throttle in context to trigger the backend context detection
        await throttle(dummy_request)
        # Check that the throttle uses the backend from the main context
        # A connection should be registered in the inmemory backend
        assert inmemory_backend.connection is not None
        assert len(inmemory_backend.connection) > 0
        # Check that the throttle backend is still left unset
        assert throttle.backend is None

        # Check that the throttle uses the backend from the inner context
        async with InMemoryBackend(persistent=True)() as inner_backend:
            await throttle(dummy_request)
            # Connection should be registered in the inner backend
            # And the main inmemory backend should still have a connection registered
            assert inner_backend.connection is not None
            assert len(inner_backend.connection) >= 1
            assert len(inmemory_backend.connection) >= 1
            # Check that the throttle backend is still left unset
            assert throttle.backend is None

        # On inner context exit, check that the throttle uses the backend from the main context again
        await throttle(dummy_request)
        # The same connection should still be registered in the main context inmemory backend,
        # and the inner context should also still have a connection registered (since it's persistent).
        assert len(inmemory_backend.connection) > 0
        assert inmemory_backend.connection is not None
        assert len(inner_backend.connection) > 0
        # Check that the throttle backend is still left unset
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.native
def test_throttle_dynamic_backend_with_lifespan_and_endpoint_context(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test dynamic backend switching with Starlette where:

    - App lifespan uses one backend (inmemory_backend)
    - Specific endpoint uses a different backend context
    """

    throttle = HTTPThrottle(
        "test-dynamic-lifespan-endpoint-starlette",
        rate="2/s",
        dynamic_backend=True,
        identifier=default_client_identifier,
    )

    # Create a separate backend for endpoint-specific usage
    endpoint_backend = InMemoryBackend(persistent=True)

    async def normal_endpoint(request):
        """Uses the lifespan backend (inmemory_backend)"""
        await throttle(request)
        return JSONResponse({"message": "normal endpoint", "backend": "lifespan"})

    async def special_endpoint(request):
        """Uses a different backend context within the endpoint"""
        async with endpoint_backend():
            await throttle(request)
            return JSONResponse({"message": "special endpoint", "backend": "endpoint"})

    routes = [
        Route("/normal", normal_endpoint, methods=["GET"]),
        Route("/special", special_endpoint, methods=["GET"]),
    ]

    # Create Starlette app with lifespan using inmemory_backend
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    base_url = "http://testserver"
    with TestClient(app, base_url=base_url) as client:
        # Test normal endpoint uses lifespan backend
        response1 = client.get("/normal")
        assert response1.status_code == 200
        assert response1.json() == {"message": "normal endpoint", "backend": "lifespan"}

        response2 = client.get("/normal")
        assert response2.status_code == 200

        # Third request to normal endpoint should be throttled (limit=2)
        response3 = client.get("/normal")
        assert response3.status_code == 429
        assert "Retry-After" in response3.headers

        # Verify the lifespan backend has a connection recorded
        assert inmemory_backend.connection is not None
        assert len(inmemory_backend.connection) >= 1
        assert (
            endpoint_backend.connection is None
        )  # No connections in endpoint backend yet

        # Test special endpoint uses different backend - should not be throttled
        # even though we hit the limit on the lifespan backend
        response4 = client.get("/special")
        assert response4.status_code == 200
        assert response4.json() == {
            "message": "special endpoint",
            "backend": "endpoint",
        }

        response5 = client.get("/special")
        assert response5.status_code == 200

        # Third request to special endpoint should be throttled in its own backend
        response6 = client.get("/special")
        assert response6.status_code == 429
        assert "Retry-After" in response6.headers

        # Verify both backends have their own separate counters
        assert len(inmemory_backend.connection) >= 1  # Still 1 from normal endpoint
        assert endpoint_backend.connection is not None
        assert len(endpoint_backend.connection) >= 1  # 1 from special endpoint

        # Verify throttle backend is still None (dynamic resolution)
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.native
def test_throttle_dynamic_backend_with_middleware(
    inmemory_backend: InMemoryBackend,
) -> None:
    # Shared throttle for all tenants
    quota_throttle = HTTPThrottle(
        uid="api_quota",
        rate="2/s",  # Short window for testing
        dynamic_backend=True,
        identifier=default_client_identifier,
    )

    # Different backends for different tenant tiers
    premium_backend = InMemoryBackend(persistent=True)
    free_backend = InMemoryBackend(persistent=True)

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Extract tenant tier from Authorization header
            auth_header = request.headers.get("authorization", "")
            if "premium" in auth_header:
                async with premium_backend():
                    response = await call_next(request)
                    return response
            elif "free" in auth_header:
                async with free_backend():
                    response = await call_next(request)
                    return response
            else:
                # Default to lifespan backend
                response = await call_next(request)
                return response

    async def data_endpoint(request):
        await quota_throttle(request)
        return JSONResponse({"data": "response"})

    routes = [Route("/api/data", data_endpoint, methods=["GET"])]
    middleware = [Middleware(TenantMiddleware)]
    # Create app with lifespan and middleware
    app = Starlette(
        routes=routes, middleware=middleware, lifespan=inmemory_backend.lifespan
    )

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        assert inmemory_backend.connection is not None
        # Test premium tenant
        premium_headers = {"authorization": "Bearer premium-token"}

        response1 = client.get("/api/data", headers=premium_headers)
        assert response1.status_code == 200

        response2 = client.get("/api/data", headers=premium_headers)
        assert response2.status_code == 200

        # Third request should be throttled for premium
        response3 = client.get("/api/data", headers=premium_headers)
        assert response3.status_code == 429

        # Verify premium backend has connections
        assert premium_backend.connection is not None
        assert len(premium_backend.connection) >= 1
        assert free_backend.connection is None
        assert len(inmemory_backend.connection) == 0

        # Test free tier (should not be throttled despite premium being throttled)
        free_headers = {"authorization": "Bearer free-token"}

        response4 = client.get("/api/data", headers=free_headers)
        assert response4.status_code == 200

        response5 = client.get("/api/data", headers=free_headers)
        assert response5.status_code == 200

        # Third request should be throttled for free tier
        response6 = client.get("/api/data", headers=free_headers)
        assert response6.status_code == 429

        # Verify all backends have separate counters
        assert free_backend.connection is not None
        assert len(premium_backend.connection) >= 1
        assert len(free_backend.connection) >= 1
        assert len(inmemory_backend.connection) == 0

        # Test default (lifespan) backend
        response7 = client.get("/api/data")
        assert response7.status_code == 200

        response8 = client.get("/api/data")
        assert response8.status_code == 200

        # Third request should be throttled for default
        response9 = client.get("/api/data")
        assert response9.status_code == 429

        # Verify all backends are isolated
        assert len(premium_backend.connection) >= 1
        assert len(free_backend.connection) >= 1
        assert len(inmemory_backend.connection) >= 1

        # Verify throttle backend is still None (dynamic resolution)
        assert quota_throttle.backend is None
