"""Tests for dynamic backend switching for throttles in FastAPI."""

import typing

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


@pytest.mark.asyncio
@pytest.mark.throttle
@pytest.mark.fastapi
async def test_throttle_dynamic_backend(inmemory_backend: InMemoryBackend) -> None:
    # This throttle should raise an error if dynamic_backend is True
    # and a backend is provided, as it should not use the backend
    with pytest.raises(ValueError):
        throttle = HTTPThrottle(
            "test-dynamic-backend-with-backend",
            rate=Rate(limit=2, seconds=50, minutes=2, hours=1),
            dynamic_backend=True,
            backend=inmemory_backend,
            registry=ThrottleRegistry(),
        )

    # This throttle should respect the context of the backend
    # and should use the backend from the context if available.
    throttle = HTTPThrottle(
        "test-dynamic-backend-no-backend",
        rate=Rate(limit=2, seconds=50, minutes=2, hours=1),
        dynamic_backend=True,
        identifier=default_client_identifier,
    )
    assert throttle.use_fixed_backend is False
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
        # use the throttle in context to trigger the backend context detection
        await throttle(dummy_request)
        # Check that the throttle backend is still left unset
        assert throttle.backend is None

        # Check that the throttle uses the backend from the inner context
        async with InMemoryBackend(persistent=True)():
            await throttle(dummy_request)
            # Check that the throttle backend is still left unset
            assert throttle.backend is None

        # On inner context exit, check that the throttle uses the backend from the main context, again
        # and not the inner context.
        await throttle(dummy_request)
        # Check that the throttle backend is still left unset
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_with_dynamic_backend_and_lifespan(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Test dynamic backend switching where:

    - App lifespan uses one backend (inmemory_backend)
    - Specific endpoint uses a different backend context
    """
    throttle = HTTPThrottle(
        "test-dynamic-lifespan-endpoint",
        rate="2/1020ms",
        dynamic_backend=True,
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )  # Create app with lifespan using `inmemory_backend`
    app = FastAPI(lifespan=inmemory_backend.lifespan)

    # Create a separate backend for endpoint-specific usage
    endpoint_backend = InMemoryBackend(persistent=True)

    @app.get("/normal", dependencies=[Depends(throttle)])
    async def normal_endpoint(request: Request) -> typing.Dict[str, str]:
        """Uses the lifespan backend (`inmemory_backend`)"""
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

        # Verify throttle backend is still None (dynamic resolution)
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_with_dynamic_backend_and_middleware(
    inmemory_backend: InMemoryBackend,
) -> None:
    # Shared throttle for all tenants
    api_quota_throttle = HTTPThrottle(
        uid="api_quota-fa",
        rate="2/min",
        dynamic_backend=True,
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )

    # Different backends for different tenant tiers
    premium_backend = InMemoryBackend(persistent=True)
    free_backend = InMemoryBackend(persistent=True)

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Extract tenant tier from Authorization header
            auth_header = request.headers.get("authorization", "")

            if "premium" in auth_header:
                async with premium_backend(close_on_exit=False):
                    response = await call_next(request)
                    return response
            elif "free" in auth_header:
                async with free_backend(close_on_exit=False):
                    response = await call_next(request)
                    return response
            else:
                # Default to lifespan backend
                response = await call_next(request)
                return response

    # Create app with lifespan and middleware
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    app.add_middleware(TenantMiddleware)  # type: ignore[arg-type]

    @app.get("/api/data", dependencies=[Depends(api_quota_throttle)])
    async def get_data() -> typing.Dict[str, str]:
        return {"data": "response"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # Test premium tenant
        premium_headers = {"authorization": "Bearer premium-token"}

        response1 = client.get("/api/data", headers=premium_headers)
        assert response1.status_code == 200

        response2 = client.get("/api/data", headers=premium_headers)
        assert response2.status_code == 200

        # Third request should be throttled for premium
        response3 = client.get("/api/data", headers=premium_headers)
        assert response3.status_code == 429

        # Test free tier (should not be throttled despite premium being throttled)
        free_headers = {"authorization": "Bearer free-token"}

        response4 = client.get("/api/data", headers=free_headers)
        assert response4.status_code == 200

        response5 = client.get("/api/data", headers=free_headers)
        assert response5.status_code == 200

        # Third request should be throttled for free tier
        response6 = client.get("/api/data", headers=free_headers)
        assert response6.status_code == 429

        # Test default (lifespan) backend
        response7 = client.get("/api/data")
        assert response7.status_code == 200

        response8 = client.get("/api/data")
        assert response8.status_code == 200

        # Third request should be throttled for default
        response9 = client.get("/api/data")
        assert response9.status_code == 429

        # Verify throttle backend is still None (dynamic resolution)
        assert api_quota_throttle.backend is None
