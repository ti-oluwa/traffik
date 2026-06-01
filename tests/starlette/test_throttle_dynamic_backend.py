"""Tests for dynamic backend switching for throttles in Starlette."""

import asyncio
import typing

import pytest
from httpx2 import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.asynctestclient import AsyncTestClient
from tests.utils import (
    default_client_identifier,
    make_connection,
    requires_throttle_type,
)
from traffik.backends.base import connection_throttled
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle, Throttle, WebSocketThrottle


@pytest.mark.anyio
@pytest.mark.throttle
@requires_throttle_type
async def test_throttle_with_dynamic_backend(
    backend: InMemoryBackend, throttle_type: typing.Type[Throttle[HTTPConnection]]
) -> None:
    """Test dynamic backend switching for `HTTPThrottle` in Starlette."""
    # This throttle should raise an error if dynamic_backend is True
    # and a backend is provided
    with pytest.raises(ValueError):
        throttle = throttle_type(
            "test-dynamic-backend-with-backend",
            rate=Rate(limit=2, hours=1),
            dynamic_backend=True,
            backend=backend,
            registry=ThrottleRegistry(),
        )

    # This throttle should respect the context of the backend
    # and should use the backend from the context if available.
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

    # Use the throttle in context to trigger the backend context detection
    await throttle(connection)
    # Check that the throttle backend is still left unset
    assert throttle.backend is None

    # Check that the throttle uses the backend from the inner context
    async with backend.__class__(persistent=True)(close_on_exit=False):
        await throttle(connection)
        # Check that the throttle backend is still left unset
        assert throttle.backend is None

    # On inner context exit, check that the throttle uses the backend from the main context again
    await throttle(connection)
    # Check that the throttle backend is still left unset
    assert throttle.backend is None


@pytest.mark.anyio
@pytest.mark.throttle
class TestHTTPThrottleDynamic:
    async def test_http_throttle_with_dynamic_backend_and_lifespan(
        self, backend: InMemoryBackend
    ) -> None:
        """
        Test dynamic backend switching with Starlette where:

        - App lifespan uses one backend (backend)
        - Specific endpoint uses a different backend context
        """
        throttle = HTTPThrottle(
            "test-dynamic-lifespan-endpoint-starlette",
            rate="2/s",
            dynamic_backend=True,
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        # Create a separate backend for endpoint-specific usage
        endpoint_backend = backend.__class__(
            namespace="endpoint",
            persistent=True,  # Ensure its state persists
        )

        async def normal_endpoint(request):
            """Uses the lifespan backend (backend)"""
            await throttle(request)
            return JSONResponse({"message": "normal endpoint", "backend": "lifespan"})

        async def special_endpoint(request):
            """Uses a different backend context within the endpoint"""
            async with endpoint_backend(
                close_on_exit=False, persistent=True
            ):  # Ensure that the connection stays alive across requests
                await throttle(request)
                return JSONResponse(
                    {"message": "special endpoint", "backend": "endpoint"}
                )

        routes = [
            Route("/normal", normal_endpoint, methods=["GET"]),
            Route("/special", special_endpoint, methods=["GET"]),
        ]
        # Create Starlette app with lifespan using backend
        app = Starlette(routes=routes)

        base_url = "http://testserver"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Test normal endpoint uses lifespan backend
            response1 = await client.get("/normal")
            assert response1.status_code == 200
            assert response1.json() == {
                "message": "normal endpoint",
                "backend": "lifespan",
            }

            response2 = await client.get("/normal")
            assert response2.status_code == 200

            # Third request to normal endpoint should be throttled (limit=2)
            response3 = await client.get("/normal")
            assert response3.status_code == 429
            assert "Retry-After" in response3.headers

            # Test special endpoint uses different backend. We should not be throttled
            # even though we hit the limit on the lifespan backend
            response4 = await client.get("/special")
            assert response4.status_code == 200
            assert response4.json() == {
                "message": "special endpoint",
                "backend": "endpoint",
            }

            response5 = await client.get("/special")
            assert response5.status_code == 200

            # Third request to special endpoint should be throttled in its own backend
            response6 = await client.get("/special")
            assert response6.status_code == 429
            assert "Retry-After" in response6.headers

            # Verify throttle backend is still None (dynamic resolution)
            assert throttle.backend is None

    async def test_http_throttle_with_dynamic_backend_and_middleware(
        self, backend: InMemoryBackend
    ) -> None:
        # Shared throttle for all tenants
        quota_throttle = HTTPThrottle(
            uid="api_quota-sl",
            rate="2/min",
            dynamic_backend=True,
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        # Different backends for different tenant tiers
        premium_backend = backend.__class__(persistent=True)
        free_backend = backend.__class__(persistent=True)

        class TenantMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
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

                # Default to lifespan backend
                response = await call_next(request)
                return response

        async def data_endpoint(request):
            await quota_throttle(request)
            return JSONResponse({"data": "response"})

        routes = [Route("/api/data", data_endpoint, methods=["GET"])]
        middleware = [Middleware(TenantMiddleware)]  # type: ignore
        # Create app with lifespan and middleware
        app = Starlette(routes=routes, middleware=middleware)

        base_url = "http://0.0.0.0"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=base_url
        ) as client:
            # Test premium tenant
            premium_headers = {"authorization": "Bearer premium-token"}

            response1 = await client.get("/api/data", headers=premium_headers)
            assert response1.status_code == 200

            response2 = await client.get("/api/data", headers=premium_headers)
            assert response2.status_code == 200

            # Third request should be throttled for premium
            response3 = await client.get("/api/data", headers=premium_headers)
            assert response3.status_code == 429

            # Test free tier (should not be throttled despite premium being throttled)
            free_headers = {"authorization": "Bearer free-token"}

            response4 = await client.get("/api/data", headers=free_headers)
            assert response4.status_code == 200

            response5 = await client.get("/api/data", headers=free_headers)
            assert response5.status_code == 200

            # Third request should be throttled for free tier
            response6 = await client.get("/api/data", headers=free_headers)
            assert response6.status_code == 429

            # Test default (lifespan) backend
            response7 = await client.get("/api/data")
            assert response7.status_code == 200

            response8 = await client.get("/api/data")
            assert response8.status_code == 200

            # Third request should be throttled for default
            response9 = await client.get("/api/data")
            assert response9.status_code == 429

            # Verify throttle backend is still None (dynamic resolution)
            assert quota_throttle.backend is None


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.websocket
class TestWebSocketThrottleDynamic:
    async def test_websocket_throttle_with_dynamic_backend_and_lifespan(
        self, backend: InMemoryBackend
    ) -> None:
        throttle = WebSocketThrottle(
            "test-ws-dynamic-lifespan",
            rate="2/5s",
            dynamic_backend=True,
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
            handle_throttled=connection_throttled
        )

        endpoint_backend = backend.__class__(
            namespace="ws-endpoint",
            persistent=True,
        )

        async def normal_ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await throttle(websocket)
            await websocket.send_json({"backend": "lifespan"})
            await websocket.close()

        async def special_ws_endpoint(websocket: WebSocket) -> None:
            async with endpoint_backend(close_on_exit=False, persistent=True):
                await websocket.accept()
                await throttle(websocket)
                await websocket.send_json({"backend": "endpoint"})
                await websocket.close()

        routes = [
            WebSocketRoute("/ws/normal", normal_ws_endpoint),
            WebSocketRoute("/ws/special", special_ws_endpoint),
        ]
        app = Starlette(routes=routes, lifespan=backend.lifespan)

        base_url = "http://0.0.0.0"
        loop = asyncio.get_running_loop()
        async with AsyncTestClient(
            app=app, base_url=base_url, event_loop=loop
        ) as client:
            # Normal endpoint: uses lifespan backend (limit 2)
            async with client.websocket_connect("/ws/normal") as ws:
                data = await ws.receive_json()
                assert data == {"backend": "lifespan"}

            async with client.websocket_connect("/ws/normal") as ws:
                data = await ws.receive_json()
                assert data == {"backend": "lifespan"}

            # Third normal connection should be throttled
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws/normal") as ws:
                    await ws.receive_json()

            # Special endpoint uses its own backend, unaffected by normal's exhaustion
            async with client.websocket_connect("/ws/special") as ws:
                data = await ws.receive_json()
                assert data == {"backend": "endpoint"}

            async with client.websocket_connect("/ws/special") as ws:
                data = await ws.receive_json()
                assert data == {"backend": "endpoint"}

            # Third special connection should be throttled in its own backend
            with pytest.raises((WebSocketDisconnect, Exception)):
                async with client.websocket_connect("/ws/special") as ws:
                    await ws.receive_json()

            assert throttle.backend is None
