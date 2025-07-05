import os
import typing

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request

from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.throttles import HTTPThrottle

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
async def app() -> FastAPI:
    app = FastAPI()
    return app


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, prefix="redis-test", persistent=False)


@pytest.fixture(scope="function")
def lifespan_app(inmemory_backend: InMemoryBackend) -> FastAPI:
    """
    Lifespan fixture for FastAPI app to ensure proper startup and shutdown.
    """
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    return app


async def _testclient_identifier(connection: HTTPConnection) -> str:
    return "testclient"


################################################
# Test dynamic backend switching for throttles #
################################################


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.throttle
@pytest.mark.fastapi
async def test_throttle_dynamic_backend(
    inmemory_backend: InMemoryBackend,
) -> None:
    # This throttle should raise an error if dynamic_backend is True
    # and a backend is provided, as it should not use the backend
    with pytest.raises(ValueError):
        throttle = HTTPThrottle(
            "test-dynamic-backend-with-backend",
            limit=2,
            milliseconds=10,
            seconds=50,
            minutes=2,
            hours=1,
            dynamic_backend=True,
            backend=inmemory_backend,
        )

    # This throttle should respect the context of the backend
    # and should use the backend from the context if available.
    throttle = HTTPThrottle(
        "test-dynamic-backend-no-backend",
        limit=2,
        milliseconds=10,
        seconds=50,
        minutes=2,
        hours=1,
        dynamic_backend=True,
        identifier=_testclient_identifier,
    )
    assert throttle.dynamic_backend is True
    assert throttle.backend is None

    dummy_request = Request(
        scope={
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
        }
    )
    async with inmemory_backend():
        # use the throttle in context to trigger the backend context detection
        await throttle(dummy_request)
        # Check that the throttle uses the backend from the main context
        # A connection should be registered in the inmemory backend
        assert inmemory_backend.connection is not None
        assert len(inmemory_backend.connection) == 1
        # Check that the throttle backend is still left unset
        assert throttle.backend is None

        # Check that the throttle uses the backend from the inner context
        async with InMemoryBackend(persistent=True)() as inner_backend:
            await throttle(dummy_request)
            # No connection should be registered in the inner backend
            # But the inmemory backend should still have one connection registered
            assert inner_backend.connection is not None
            assert len(inner_backend.connection) == 1
            assert len(inmemory_backend.connection) == 1
            # Check that the throttle backend is still left unset
            assert throttle.backend is None

        # On inner context exit, check that the throttle uses the backend from the main context, again
        # and not the inner context.
        await throttle(dummy_request)
        # The same connection should still be registered in the main context inmemory backend,
        # and the inner context should also still have one connection registered (since its persistent).
        assert len(inmemory_backend.connection) == 1
        assert len(inner_backend.connection) == 1
        # Check that the throttle backend is still left unset
        assert throttle.backend is None


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dynamic_backend_with_lifespan_and_endpoint_context(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test dynamic backend switching where:

    - App lifespan uses one backend (inmemory_backend)
    - Specific endpoint uses a different backend context
    """
    throttle = HTTPThrottle(
        "test-dynamic-lifespan-endpoint",
        limit=2,
        milliseconds=200,
        dynamic_backend=True,
        identifier=_testclient_identifier,
    )

    # Create app with lifespan using inmemory_backend
    app = FastAPI(lifespan=inmemory_backend.lifespan)

    # Create a separate backend for endpoint-specific usage
    endpoint_backend = InMemoryBackend(persistent=True)

    @app.get("/normal", dependencies=[Depends(throttle)])
    async def normal_endpoint(request: Request) -> typing.Dict[str, str]:
        """Uses the lifespan backend (inmemory_backend)"""
        return {"message": "normal endpoint", "backend": "lifespan"}

    @app.get("/special")
    async def special_endpoint(request: Request) -> typing.Dict[str, str]:
        """Uses a different backend context within the endpoint"""
        async with endpoint_backend(close_on_exit=False):
            await throttle(request)
            return {"message": "special endpoint", "backend": "endpoint"}

    base_url = "http://0.0.0.0"
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

        # Verify the lifespan backend has 1 connection recorded
        assert inmemory_backend.connection is not None
        assert len(inmemory_backend.connection) == 1
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
        assert len(inmemory_backend.connection) == 1  # Still 1 from normal endpoint
        assert endpoint_backend.connection is not None
        assert len(endpoint_backend.connection) == 1  # 1 from special endpoint

        # Verify throttle backend is still None (dynamic resolution)
        assert throttle.backend is None


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dynamic_backend_with_middleware(
    inmemory_backend: InMemoryBackend,
) -> None:
    # Shared throttle for all tenants
    api_quota_throttle = HTTPThrottle(
        uid="api_quota",
        limit=2,
        milliseconds=100,  # Short window for testing
        dynamic_backend=True,
        identifier=_testclient_identifier,
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

    # Create app with lifespan and middleware
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(TenantMiddleware)

    @app.get("/api/data", dependencies=[Depends(api_quota_throttle)])
    async def get_data() -> typing.Dict[str, str]:
        return {"data": "response"}

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
        assert len(premium_backend.connection) == 1
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
        assert len(premium_backend.connection) == 1
        assert len(free_backend.connection) == 1
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
        assert len(premium_backend.connection) == 1
        assert len(free_backend.connection) == 1
        assert len(inmemory_backend.connection) == 1

        # Verify throttle backend is still None (dynamic resolution)
        assert api_quota_throttle.backend is None


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dynamic_backend_environment_switching(
    inmemory_backend: InMemoryBackend,
) -> None:
    # Throttle for environment-based rate limiting
    env_throttle = HTTPThrottle(
        uid="environment_throttle",
        limit=2,
        milliseconds=100,
        dynamic_backend=True,
        identifier=_testclient_identifier,
    )

    # Different backends for different environments
    staging_backend = InMemoryBackend(persistent=True)
    production_backend = InMemoryBackend(persistent=True)

    class EnvironmentMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            env = request.headers.get("x-environment", "default")

            if env == "staging":
                async with staging_backend():
                    response = await call_next(request)
                    return response
            elif env == "production":
                async with production_backend():
                    response = await call_next(request)
                    return response
            else:
                # Default environment uses lifespan backend
                response = await call_next(request)
                return response

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(EnvironmentMiddleware)

    @app.get("/health", dependencies=[Depends(env_throttle)])
    async def health_check() -> typing.Dict[str, str]:
        return {"status": "healthy"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        assert inmemory_backend.connection is not None
        # Test staging environment
        staging_headers = {"x-environment": "staging"}

        response1 = client.get("/health", headers=staging_headers)
        assert response1.status_code == 200

        response2 = client.get("/health", headers=staging_headers)
        assert response2.status_code == 200

        # Third request should be throttled
        response3 = client.get("/health", headers=staging_headers)
        assert response3.status_code == 429

        # Test production environment (isolated from staging)
        prod_headers = {"x-environment": "production"}

        response4 = client.get("/health", headers=prod_headers)
        assert response4.status_code == 200

        response5 = client.get("/health", headers=prod_headers)
        assert response5.status_code == 200

        # Third request should be throttled in production
        response6 = client.get("/health", headers=prod_headers)
        assert response6.status_code == 429

        # Verify environment isolation
        assert staging_backend.connection is not None
        assert len(staging_backend.connection) == 1
        assert production_backend.connection is not None
        assert len(production_backend.connection) == 1
        assert len(inmemory_backend.connection) == 0

        # Test default environment
        response7 = client.get("/health")
        assert response7.status_code == 200

        response8 = client.get("/health")
        assert response8.status_code == 200

        response9 = client.get("/health")
        assert response9.status_code == 429

        # All environments should be isolated
        assert len(staging_backend.connection) == 1
        assert len(production_backend.connection) == 1
        assert len(inmemory_backend.connection) == 1

        # Verify dynamic resolution
        assert env_throttle.backend is None


@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dynamic_backend_nested_contexts(
    inmemory_backend: InMemoryBackend,
) -> None:
    # Multiple throttles for different purposes
    api_throttle = HTTPThrottle(
        uid="api_requests",
        limit=3,
        milliseconds=100,
        dynamic_backend=True,
        identifier=_testclient_identifier,
    )

    feature_throttle = HTTPThrottle(
        uid="feature_usage",
        limit=2,
        milliseconds=100,
        dynamic_backend=True,
        identifier=_testclient_identifier,
    )

    # Different tenant backends
    tenant_a_backend = InMemoryBackend(persistent=True)
    tenant_b_backend = InMemoryBackend(persistent=True)

    class TenantRoutingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Extract tenant from path
            path_parts = request.url.path.split("/")

            if len(path_parts) >= 3 and path_parts[1] == "tenant":
                tenant_id = path_parts[2]

                if tenant_id == "tenant-a":
                    async with tenant_a_backend():
                        response = await call_next(request)
                        return response
                elif tenant_id == "tenant-b":
                    async with tenant_b_backend():
                        response = await call_next(request)
                        return response

            # Default to lifespan backend
            response = await call_next(request)
            return response

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(TenantRoutingMiddleware)

    @app.get("/tenant/{tenant_id}/api", dependencies=[Depends(api_throttle)])
    async def tenant_api(tenant_id: str) -> typing.Dict[str, str]:
        return {"tenant": tenant_id, "service": "api"}

    @app.get("/tenant/{tenant_id}/feature", dependencies=[Depends(feature_throttle)])
    async def tenant_feature(tenant_id: str) -> typing.Dict[str, str]:
        return {"tenant": tenant_id, "service": "feature"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        assert inmemory_backend.connection is not None
        # Test tenant-a API throttle
        for i in range(3):
            response = client.get("/tenant/tenant-a/api")
            assert response.status_code == 200

        # Fourth request should be throttled
        response = client.get("/tenant/tenant-a/api")
        assert response.status_code == 429

        # Test tenant-a feature throttle (different throttle, should work)
        for i in range(2):
            response = client.get("/tenant/tenant-a/feature")
            assert response.status_code == 200

        # Third feature request should be throttled
        response = client.get("/tenant/tenant-a/feature")
        assert response.status_code == 429

        # Test tenant-b (should not be affected by tenant-a limits)
        for i in range(3):
            response = client.get("/tenant/tenant-b/api")
            assert response.status_code == 200

        response = client.get("/tenant/tenant-b/api")
        assert response.status_code == 429

        # Verify backend isolation
        # Each tenant should have 2 throttle records (api + feature)
        assert tenant_a_backend.connection is not None
        assert len(tenant_a_backend.connection) == 2
        assert tenant_b_backend.connection is not None
        assert len(tenant_b_backend.connection) == 1  # Only tested API
        assert len(inmemory_backend.connection) == 0

        # Verify throttles remain dynamic
        assert api_throttle.backend is None
        assert feature_throttle.backend is None
