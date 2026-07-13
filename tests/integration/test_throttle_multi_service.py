"""
Tests for throttle sharing/isolation across multiple independent ASGI services.
"""

import asyncio
import typing

import pytest
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse

from tests.client import AsyncTestClient
from tests.conftest import BackendGen
from tests.frameworks import ASGIFramework, HTTPRoute
from tests.utils import default_client_identifier, make_client
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


async def tenant_identifier(connection: HTTPConnection) -> str:
    auth_header = connection.headers.get("authorization", "")
    if "tenant-a" in auth_header:
        return "tenant-a"
    elif "tenant-b" in auth_header:
        return "tenant-b"
    return "unknown"


@pytest.mark.throttle
@pytest.mark.anyio
class TestMultiServicePatterns:
    async def test_multi_service_shared_backend(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """
        Throttles with the same UID AND same path share limits across
        independently-built services on the same backend.
        """
        for backend in backends(persistent=False, namespace="shared_backend_test"):
            async with backend(close_on_exit=True):
                shared_throttle_config = {
                    "uid": "shared_api_limit",
                    "rate": "3/min",
                    "identifier": default_client_identifier,
                }

                throttle_a = HTTPThrottle(
                    **shared_throttle_config, registry=ThrottleRegistry()
                )

                async def service_a_endpoint(request: Request) -> JSONResponse:
                    await throttle_a(request)
                    return JSONResponse({"service": "A", "data": "response"})

                app_a = web_framework.build_app(
                    http_routes=[HTTPRoute("/api/shared", service_a_endpoint)]
                )

                throttle_b = HTTPThrottle(
                    **shared_throttle_config, registry=ThrottleRegistry()
                )

                async def service_b_endpoint(request: Request) -> JSONResponse:
                    await throttle_b(request)
                    return JSONResponse({"service": "B", "data": "response"})

                app_b = web_framework.build_app(
                    http_routes=[HTTPRoute("/api/shared", service_b_endpoint)]
                )

                async with (
                    make_client(app_a, base_url="http://0.0.0.0") as client_a,
                    make_client(app_b, base_url="http://0.0.0.0") as client_b,
                ):
                    # 2 requests to service A
                    assert (await client_a.get("/api/shared")).status_code == 200
                    assert (await client_a.get("/api/shared")).status_code == 200

                    # 1 request to service B (counts toward the shared limit)
                    assert (await client_b.get("/api/shared")).status_code == 200

                    # 4th request to either service should be throttled (limit=3)
                    response4 = await client_a.get("/api/shared")
                    assert response4.status_code == 429
                    assert "Retry-After" in response4.headers

                    response5 = await client_b.get("/api/shared")
                    assert response5.status_code == 429
                    assert "Retry-After" in response5.headers

    async def test_multi_service_path_isolation(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """Same UID but different paths get separate limits -- path is part of the key."""
        for backend in backends(persistent=False, namespace="path_isolation_test"):
            async with backend(close_on_exit=True):
                shared_uid_config = {
                    "uid": "shared_uid",
                    "rate": "2/min",
                    "identifier": default_client_identifier,
                }

                throttle_a = HTTPThrottle(
                    **shared_uid_config, registry=ThrottleRegistry()
                )

                async def service_a_endpoint(request: Request) -> JSONResponse:
                    await throttle_a(request)
                    return JSONResponse({"service": "A", "data": "response"})

                app_a = web_framework.build_app(
                    http_routes=[HTTPRoute("/service-a/data", service_a_endpoint)]
                )

                throttle_b = HTTPThrottle(
                    **shared_uid_config, registry=ThrottleRegistry()
                )

                async def service_b_endpoint(request: Request) -> JSONResponse:
                    await throttle_b(request)
                    return JSONResponse({"service": "B", "data": "response"})

                app_b = web_framework.build_app(
                    http_routes=[HTTPRoute("/service-b/data", service_b_endpoint)]
                )

                async with (
                    make_client(app_a, base_url="http://0.0.0.0") as client_a,
                    make_client(app_b, base_url="http://0.0.0.0") as client_b,
                ):
                    # Exhaust service A's limit
                    assert (await client_a.get("/service-a/data")).status_code == 200
                    assert (await client_a.get("/service-a/data")).status_code == 200
                    assert (await client_a.get("/service-a/data")).status_code == 429

                    # Service B has a separate limit (different path)
                    assert (await client_b.get("/service-b/data")).status_code == 200
                    assert (await client_b.get("/service-b/data")).status_code == 200
                    assert (await client_b.get("/service-b/data")).status_code == 429

                    # Service A is still throttled
                    assert (await client_a.get("/service-a/data")).status_code == 429

    async def test_microservices_pattern(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """
        Realistic pattern: two services handling the same logical operation on
        the same path with the same UID truly share one limit, per identifier.
        """
        for backend in backends(persistent=False, namespace="microservices_test"):
            async with backend(close_on_exit=True):
                user_throttle = HTTPThrottle(
                    uid="tenant_api_operations",
                    rate="3/min",
                    identifier=tenant_identifier,
                    registry=ThrottleRegistry(),
                )

                async def get_user_operations(request: Request) -> JSONResponse:
                    await user_throttle(request)
                    return JSONResponse({"service": "user", "operation": "data_access"})

                user_app = web_framework.build_app(
                    http_routes=[HTTPRoute("/api/operations", get_user_operations)]
                )

                order_throttle = HTTPThrottle(
                    uid="tenant_api_operations",  # Same UID
                    rate="3/min",
                    identifier=tenant_identifier,
                    registry=ThrottleRegistry(),
                )

                async def get_order_operations(request: Request) -> JSONResponse:
                    await order_throttle(request)
                    return JSONResponse(
                        {
                            "service": "order",
                            "operation": "data_access",
                        }
                    )

                order_app = web_framework.build_app(
                    http_routes=[HTTPRoute("/api/operations", get_order_operations)]
                )

                async with (
                    make_client(user_app, base_url="http://0.0.0.0") as user_client,
                    make_client(order_app, base_url="http://0.0.0.0") as order_client,
                ):
                    tenant_a_headers = {"authorization": "Bearer tenant-a-token"}
                    tenant_b_headers = {"authorization": "Bearer tenant-b-token"}

                    # Tenant A across both services (shared path: /api/operations)
                    assert (
                        await user_client.get(
                            "/api/operations", headers=tenant_a_headers
                        )
                    ).status_code == 200
                    assert (
                        await order_client.get(
                            "/api/operations", headers=tenant_a_headers
                        )
                    ).status_code == 200
                    assert (
                        await user_client.get(
                            "/api/operations", headers=tenant_a_headers
                        )
                    ).status_code == 200

                    # 4th request for tenant A is throttled (shared limit=3)
                    response4 = await order_client.get(
                        "/api/operations", headers=tenant_a_headers
                    )
                    assert response4.status_code == 429

                    # Tenant B has a separate limit (different identifier)
                    assert (
                        await user_client.get(
                            "/api/operations", headers=tenant_b_headers
                        )
                    ).status_code == 200
                    assert (
                        await order_client.get(
                            "/api/operations", headers=tenant_b_headers
                        )
                    ).status_code == 200
                    assert (
                        await user_client.get(
                            "/api/operations", headers=tenant_b_headers
                        )
                    ).status_code == 200

                    response8 = await order_client.get(
                        "/api/operations", headers=tenant_b_headers
                    )
                    assert response8.status_code == 429

    async def test_multi_service_endpoint_isolation(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """
        Different endpoints in the same service get isolated limits even with
        the same UID, since path is part of the throttle key.
        """
        for backend in backends(persistent=False, namespace="endpoint_isolation_test"):
            async with backend(close_on_exit=True):
                shared_uid = "endpoint_specific_limits"
                read_throttle = HTTPThrottle(
                    uid=shared_uid,
                    rate="2/1050ms",
                    identifier=default_client_identifier,
                    registry=ThrottleRegistry(),
                )
                write_throttle = HTTPThrottle(
                    uid=shared_uid,  # Same UID, different limit
                    rate="1/1050ms",
                    identifier=default_client_identifier,
                    registry=ThrottleRegistry(),
                )

                async def read_endpoint(request: Request) -> JSONResponse:
                    await read_throttle(request)
                    return JSONResponse({"operation": "read", "status": "success"})

                async def write_endpoint(request: Request) -> JSONResponse:
                    await write_throttle(request)
                    return JSONResponse({"operation": "write", "status": "success"})

                app = web_framework.build_app(
                    http_routes=[
                        HTTPRoute("/api/read", read_endpoint, methods=["GET"]),
                        HTTPRoute("/api/write", write_endpoint, methods=["POST"]),
                    ]
                )

                async with make_client(app, base_url="http://0.0.0.0") as client:
                    assert (await client.get("/api/read")).status_code == 200
                    assert (await client.get("/api/read")).status_code == 200
                    assert (await client.get("/api/read")).status_code == 429

                    # Write endpoint has a separate limit (different path)
                    assert (await client.post("/api/write")).status_code == 200
                    assert (await client.post("/api/write")).status_code == 429

                    # Read is still independently throttled
                    assert (await client.get("/api/read")).status_code == 429

    async def test_multi_service_namespace_isolation(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """Same UID on backends with different namespaces are fully isolated."""
        for backend_a, backend_b in zip(
            backends(persistent=False, namespace="backend-a"),
            backends(persistent=False, namespace="backend-b"),
        ):
            async with backend_a(close_on_exit=True), backend_b(close_on_exit=True):
                throttle_a = HTTPThrottle(
                    uid="api_limit",  # Same UID, different namespaces
                    rate="2/s",
                    identifier=default_client_identifier,
                    backend=backend_a,
                    registry=ThrottleRegistry(),
                )

                async def service_a_data(request: Request) -> JSONResponse:
                    await throttle_a(request)
                    return JSONResponse({"service": "A", "data": "response"})

                app_a = web_framework.build_app(
                    http_routes=[HTTPRoute("/data", service_a_data)]
                )

                throttle_b = HTTPThrottle(
                    uid="api_limit",  # Same UID, different namespaces
                    rate="2/s",
                    identifier=default_client_identifier,
                    backend=backend_b,
                    registry=ThrottleRegistry(),
                )

                async def service_b_data(request: Request) -> JSONResponse:
                    await throttle_b(request)
                    return JSONResponse({"service": "B", "data": "response"})

                app_b = web_framework.build_app(
                    http_routes=[HTTPRoute("/data", service_b_data)]
                )

                async with (
                    make_client(app_a, base_url="http://0.0.0.0") as client_a,
                    make_client(app_b, base_url="http://0.0.0.0") as client_b,
                ):
                    assert (await client_a.get("/data")).status_code == 200
                    assert (await client_a.get("/data")).status_code == 200
                    assert (await client_a.get("/data")).status_code == 429

                    # Service B has its own independent limit (different namespace)
                    assert (await client_b.get("/data")).status_code == 200
                    assert (await client_b.get("/data")).status_code == 200
                    assert (await client_b.get("/data")).status_code == 429

    async def test_multi_service_concurrency(
        self, backends: BackendGen, web_framework: ASGIFramework
    ) -> None:
        """Concurrent requests across services sharing a limit stay consistent."""
        for backend in backends(
            persistent=False, namespace="multi_service_concurrency_test"
        ):
            async with backend(close_on_exit=True):
                shared_config = {
                    "uid": "concurrent_test",
                    "rate": "6/min",
                    "identifier": default_client_identifier,
                }

                throttle_a = HTTPThrottle(**shared_config, registry=ThrottleRegistry())

                async def service_a_endpoint(request: Request) -> JSONResponse:
                    await throttle_a(request)
                    return JSONResponse({"service": "A"})

                app_a = web_framework.build_app(
                    http_routes=[HTTPRoute("/endpoint", service_a_endpoint)]
                )

                throttle_b = HTTPThrottle(**shared_config, registry=ThrottleRegistry())

                async def service_b_endpoint(request: Request) -> JSONResponse:
                    await throttle_b(request)
                    return JSONResponse({"service": "B"})

                app_b = web_framework.build_app(
                    http_routes=[HTTPRoute("/endpoint", service_b_endpoint)]
                )

                throttle_c = HTTPThrottle(**shared_config, registry=ThrottleRegistry())

                async def service_c_endpoint(request: Request) -> JSONResponse:
                    await throttle_c(request)
                    return JSONResponse({"service": "C"})

                app_c = web_framework.build_app(
                    http_routes=[HTTPRoute("/endpoint", service_c_endpoint)]
                )

                async with (
                    make_client(app_a, base_url="http://0.0.0.0") as client_a,
                    make_client(app_b, base_url="http://0.0.0.0") as client_b,
                    make_client(app_c, base_url="http://0.0.0.0") as client_c,
                ):

                    async def make_request(
                        client: AsyncTestClient, service_name: str
                    ) -> typing.Dict[str, typing.Any]:
                        try:
                            response = await client.get("/endpoint")
                            return {
                                "service": service_name,
                                "status": response.status_code,
                            }
                        except Exception as exc:
                            return {
                                "service": service_name,
                                "status": "error",
                                "error": str(exc),
                            }

                    tasks = []
                    for i in range(10):
                        if i % 3 == 0:
                            tasks.append(make_request(client_a, "A"))
                        elif i % 3 == 1:
                            tasks.append(make_request(client_b, "B"))
                        else:
                            tasks.append(make_request(client_c, "C"))

                    results = await asyncio.gather(*tasks)

                    success_count = sum(1 for r in results if r["status"] == 200)
                    throttled_count = sum(1 for r in results if r["status"] == 429)

                    # Limit is 6, so exactly 6 succeed and 4 are throttled
                    assert success_count == 6, (
                        f"Expected 6 successful requests, got {success_count}"
                    )
                    assert throttled_count == 4, (
                        f"Expected 4 throttled requests, got {throttled_count}"
                    )

                    services_used = {
                        r["service"] for r in results if r["status"] == 200
                    }
                    assert len(services_used) >= 2, (
                        "Successful requests should come from multiple services"
                    )
