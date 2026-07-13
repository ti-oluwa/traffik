"""
Tests for `dynamic_backend=True` throttles i.e, backend resolved from context at
call time instead of being fixed on the throttle instance.
"""

import typing

import pytest
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse

from tests.frameworks import ASGIFramework, HTTPRoute
from tests.utils import (
    default_client_identifier,
    make_client,
    make_connection,
    requires_throttle_type,
)
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, Throttle


@pytest.mark.throttle
@requires_throttle_type
@pytest.mark.anyio
async def test_dynamic_backend_resolves_from_context(
    inmemory_backend: InMemoryBackend,
    throttle_type: typing.Type[Throttle[HTTPConnection]],
) -> None:
    """
    A `dynamic_backend=True` throttle:
    - rejects a `backend=` at construction (contradiction - dynamic means
      "figure it out at call time", so a fixed backend makes no sense).
    - stays unset (`throttle.backend is None`) even after being called,
      picking up whatever backend context is active for that call instead.
    - correctly switches back to the outer context once an inner one exits.

    No app involved - calls the throttle directly against a bare connection,
    so this only needs to vary by `throttle_type`, not by framework.
    """
    with pytest.raises(ValueError):
        throttle_type(
            "test-dynamic-backend-with-backend",
            rate=Rate(limit=2, hours=1),
            dynamic_backend=True,
            backend=inmemory_backend,
            registry=ThrottleRegistry(),
        )

    throttle = throttle_type(
        "test-dynamic-backend-no-backend",
        rate=Rate(limit=2, hours=1),
        dynamic_backend=True,
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )
    assert throttle.use_fixed_backend is False
    assert throttle.backend is None

    connection = make_connection(
        throttle_type.connection_type, method="GET", path="/test"
    )

    async with inmemory_backend(close_on_exit=True):
        await throttle(connection)
        assert throttle.backend is None

        async with InMemoryBackend(persistent=True)(close_on_exit=False):
            await throttle(connection)
            assert throttle.backend is None

        # Inner context exited - back to the outer one, not left dangling.
        await throttle(connection)
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.anyio
async def test_dynamic_backend_with_lifespan(
    inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
) -> None:
    """
    App lifespan binds one backend; a specific endpoint opens a second backend
    context of its own. The dynamic throttle should follow whichever context is
    active for each call, not the app's lifespan backend unconditionally.
    """
    throttle = HTTPThrottle(
        f"test-dynamic-lifespan-{web_framework.name}",
        rate="2/1020ms",
        dynamic_backend=True,
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )
    endpoint_backend = InMemoryBackend(persistent=True)

    async def normal_endpoint(request: Request) -> JSONResponse:
        """Uses the lifespan-bound backend."""
        await throttle(request)
        return JSONResponse({"message": "normal endpoint", "backend": "lifespan"})

    async def special_endpoint(request: Request) -> JSONResponse:
        """Opens its own backend context, independent of the lifespan one."""
        async with endpoint_backend(close_on_exit=False):
            await throttle(request)
            return JSONResponse({"message": "special endpoint", "backend": "endpoint"})

    app = web_framework.build_app(
        http_routes=[
            HTTPRoute("/normal", normal_endpoint),
            HTTPRoute("/special", special_endpoint),
        ],
        lifespan=inmemory_backend.lifespan,
    )

    async with make_client(app, base_url="http://0.0.0.0") as client:
        response1 = await client.get("/normal")
        assert response1.status_code == 200
        assert response1.json() == {"message": "normal endpoint", "backend": "lifespan"}

        response2 = await client.get("/normal")
        assert response2.status_code == 200

        # 3rd request to /normal should be throttled (limit=2 on the lifespan backend)
        response3 = await client.get("/normal")
        assert response3.status_code == 429
        assert "Retry-After" in response3.headers

        # /special uses a separate backend - unaffected by /normal's exhaustion
        response4 = await client.get("/special")
        assert response4.status_code == 200
        assert response4.json() == {
            "message": "special endpoint",
            "backend": "endpoint",
        }

        response5 = await client.get("/special")
        assert response5.status_code == 200

        # 3rd request to /special is throttled in its own backend
        response6 = await client.get("/special")
        assert response6.status_code == 429
        assert "Retry-After" in response6.headers

        # Dynamic resolution means the throttle itself never pins a backend.
        assert throttle.backend is None


@pytest.mark.throttle
@pytest.mark.anyio
async def test_dynamic_backend_with_middleware(
    inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
) -> None:
    """
    Middleware picks a per-tenant backend context based on a header, before the
    throttled route runs - a common "one throttle, many isolated buckets"
    pattern for multi-tenant rate limiting.
    """
    quota_throttle = HTTPThrottle(
        uid=f"api-quota-{web_framework.name}",
        rate="2/min",
        dynamic_backend=True,
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )
    premium_backend = InMemoryBackend(persistent=True)
    free_backend = InMemoryBackend(persistent=True)

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            auth_header = request.headers.get("authorization", "")
            if "premium" in auth_header:
                async with premium_backend(close_on_exit=False):
                    return await call_next(request)
            elif "free" in auth_header:
                async with free_backend(close_on_exit=False):
                    return await call_next(request)
            # No matching tier - fall through to the lifespan-bound backend.
            return await call_next(request)

    async def data_endpoint(request: Request) -> JSONResponse:
        await quota_throttle(request)
        return JSONResponse({"data": "response"})

    app = web_framework.build_app(
        http_routes=[HTTPRoute("/api/data", data_endpoint)],
        lifespan=inmemory_backend.lifespan,
        middleware=[Middleware(TenantMiddleware)],
    )

    async with make_client(app, base_url="http://0.0.0.0") as client:
        premium_headers = {"authorization": "Bearer premium-token"}
        assert (
            await client.get("/api/data", headers=premium_headers)
        ).status_code == 200
        assert (
            await client.get("/api/data", headers=premium_headers)
        ).status_code == 200
        assert (
            await client.get("/api/data", headers=premium_headers)
        ).status_code == 429

        # Free tier is unaffected - separate backend context, own budget.
        free_headers = {"authorization": "Bearer free-token"}
        assert (await client.get("/api/data", headers=free_headers)).status_code == 200
        assert (await client.get("/api/data", headers=free_headers)).status_code == 200
        assert (await client.get("/api/data", headers=free_headers)).status_code == 429

        # No auth header - falls through to the lifespan-bound default backend.
        assert (await client.get("/api/data")).status_code == 200
        assert (await client.get("/api/data")).status_code == 200
        assert (await client.get("/api/data")).status_code == 429

        assert quota_throttle.backend is None
