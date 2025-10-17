import asyncio

from httpx import ASGITransport, AsyncClient
import pytest
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.conftest import BackendGen
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle
from tests.utils import default_client_identifier


@pytest.mark.anyio
@pytest.mark.throttle
async def test_multi_service_shared_backend(backends: BackendGen) -> None:
    """
    Test multiple Starlette services sharing the same backend.
    Throttles with same UID AND same path should share limits across services.
    """
    for backend in backends(persistent=False, namespace="shared_backend_test"):
        async with backend(close_on_exit=True):
            # Shared throttle configuration across services
            shared_throttle_config = {
                "uid": "shared_api_limit",
                "rate": "3/min",
                "identifier": default_client_identifier,
            }

            # Service A
            throttle_a = HTTPThrottle(**shared_throttle_config)

            async def service_a_endpoint(request):
                await throttle_a(request)
                return JSONResponse({"service": "A", "data": "response"})

            routes_a = [Route("/api/shared", service_a_endpoint, methods=["GET"])]
            app_a = Starlette(routes=routes_a)

            # Service B
            throttle_b = HTTPThrottle(**shared_throttle_config)

            async def service_b_endpoint(request):
                await throttle_b(request)
                return JSONResponse({"service": "B", "data": "response"})

            routes_b = [Route("/api/shared", service_b_endpoint, methods=["GET"])]
            app_b = Starlette(routes=routes_b)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url=base_url
            ) as client_a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url=base_url
            ) as client_b:
                # Make 2 requests to service A
                response1 = await client_a.get("/api/shared")
                assert response1.status_code == 200

                response2 = await client_a.get("/api/shared")
                assert response2.status_code == 200

                # Make 1 request to service B (should count toward shared limit)
                response3 = await client_b.get("/api/shared")
                assert response3.status_code == 200

                # Fourth request to either service should be throttled (limit=3)
                response4 = await client_a.get("/api/shared")
                assert response4.status_code == 429
                assert "Retry-After" in response4.headers

                # Verify service B is also throttled
                response5 = await client_b.get("/api/shared")
                assert response5.status_code == 429
                assert "Retry-After" in response5.headers


@pytest.mark.anyio
@pytest.mark.throttle
async def test_multi_service_path_isolation(backends: BackendGen) -> None:
    """
    Test that services with same UID but different paths have separate limits.
    This demonstrates that path is part of the key generation.
    """
    for backend in backends(persistent=False, namespace="path_isolation_test"):
        async with backend(close_on_exit=True):
            # Same UID but different endpoints
            shared_uid_config = {
                "uid": "shared_uid",
                "rate": "2/min",
                "identifier": default_client_identifier,
            }

            # Service A
            throttle_a = HTTPThrottle(**shared_uid_config)

            async def service_a_endpoint(request):
                await throttle_a(request)
                return JSONResponse({"service": "A", "data": "response"})

            routes_a = [Route("/service-a/data", service_a_endpoint, methods=["GET"])]
            app_a = Starlette(routes=routes_a)

            # Service B
            throttle_b = HTTPThrottle(**shared_uid_config)

            async def service_b_endpoint(request):
                await throttle_b(request)
                return JSONResponse({"service": "B", "data": "response"})

            routes_b = [Route("/service-b/data", service_b_endpoint, methods=["GET"])]
            app_b = Starlette(routes=routes_b)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url=base_url
            ) as client_a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url=base_url
            ) as client_b:
                # Exhaust service A's limit (different path: /service-a/data)
                response1 = await client_a.get("/service-a/data")
                assert response1.status_code == 200

                response2 = await client_a.get("/service-a/data")
                assert response2.status_code == 200

                # Third request to service A should be throttled
                response3 = await client_a.get("/service-a/data")
                assert response3.status_code == 429

                # Service B should have separate limit (different path: /service-b/data)
                response4 = await client_b.get("/service-b/data")
                assert response4.status_code == 200

                response5 = await client_b.get("/service-b/data")
                assert response5.status_code == 200

                # Third request to service B should be throttled
                response6 = await client_b.get("/service-b/data")
                assert response6.status_code == 429

                # Verify service A is still throttled
                response7 = await client_a.get("/service-a/data")
                assert response7.status_code == 429


@pytest.mark.anyio
@pytest.mark.throttle
async def test_microservices_pattern(backends: BackendGen) -> None:
    """
    Test realistic microservices pattern where services handle the same logical operation
    but on different endpoints. Use same UID + same logical path for true sharing.
    """

    async def tenant_identifier(connection: HTTPConnection) -> str:
        auth_header = connection.headers.get("authorization", "")
        if "tenant-a" in auth_header:
            return "tenant-a"
        elif "tenant-b" in auth_header:
            return "tenant-b"
        return "unknown"

    for backend in backends(persistent=False, namespace="microservices_test"):
        async with backend(close_on_exit=True):
            # User Service
            user_throttle = HTTPThrottle(
                uid="tenant_api_operations",
                rate="3/min",
                identifier=tenant_identifier,
            )

            async def get_user_operations(request):
                await user_throttle(request)
                return JSONResponse({"service": "user", "operation": "data_access"})

            user_routes = [
                Route("/api/operations", get_user_operations, methods=["GET"])
            ]
            user_app = Starlette(routes=user_routes)

            # Order Service
            order_throttle = HTTPThrottle(
                uid="tenant_api_operations",  # Same UID
                rate="3/min",
                identifier=tenant_identifier,
            )

            async def get_order_operations(request):
                await order_throttle(request)
                return JSONResponse({"service": "order", "operation": "data_access"})

            order_routes = [
                Route("/api/operations", get_order_operations, methods=["GET"])
            ]
            order_app = Starlette(routes=order_routes)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=user_app), base_url=base_url
            ) as user_client, AsyncClient(
                transport=ASGITransport(app=order_app), base_url=base_url
            ) as order_client:
                tenant_a_headers = {"authorization": "Bearer tenant-a-token"}
                tenant_b_headers = {"authorization": "Bearer tenant-b-token"}

                # Make requests for tenant A across both services (shared path: /api/operations)
                response1 = await user_client.get(
                    "/api/operations", headers=tenant_a_headers
                )
                assert response1.status_code == 200

                response2 = await order_client.get(
                    "/api/operations", headers=tenant_a_headers
                )
                assert response2.status_code == 200

                response3 = await user_client.get(
                    "/api/operations", headers=tenant_a_headers
                )
                assert response3.status_code == 200

                # Fourth request for tenant A should be throttled (shared limit=3)
                response4 = await order_client.get(
                    "/api/operations", headers=tenant_a_headers
                )
                assert response4.status_code == 429

                # Tenant B should have separate limits (different identifier)
                response5 = await user_client.get(
                    "/api/operations", headers=tenant_b_headers
                )
                assert response5.status_code == 200

                response6 = await order_client.get(
                    "/api/operations", headers=tenant_b_headers
                )
                assert response6.status_code == 200

                response7 = await user_client.get(
                    "/api/operations", headers=tenant_b_headers
                )
                assert response7.status_code == 200

                # Fourth request for tenant B should be throttled
                response8 = await order_client.get(
                    "/api/operations", headers=tenant_b_headers
                )
                assert response8.status_code == 429


@pytest.mark.anyio
@pytest.mark.throttle
async def test_multi_service_endpoint_isolation(backends: BackendGen) -> None:
    """
    Test that different endpoints in the same service that have separate limits
    are isolated, even with the same UID (due to different paths).
    """
    for backend in backends(persistent=False, namespace="endpoint_isolation_test"):
        async with backend(close_on_exit=True):
            # Same UID for both endpoints
            shared_uid = "endpoint_specific_limits"
            read_throttle = HTTPThrottle(
                uid=shared_uid,
                rate="2/s",
                identifier=default_client_identifier,
            )
            write_throttle = HTTPThrottle(
                uid=shared_uid,  # Same UID
                # Different limit to test isolation
                rate="1/s",
                identifier=default_client_identifier,
            )

            async def read_endpoint(request):
                await read_throttle(request)
                return JSONResponse({"operation": "read", "status": "success"})

            async def write_endpoint(request):
                await write_throttle(request)
                return JSONResponse({"operation": "write", "status": "success"})

            routes = [
                Route("/api/read", read_endpoint, methods=["GET"]),
                Route("/api/write", write_endpoint, methods=["POST"]),
            ]
            app = Starlette(routes=routes)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=base_url
            ) as client:
                # Test read endpoint (limit=2)
                response1 = await client.get("/api/read")
                assert response1.status_code == 200

                response2 = await client.get("/api/read")
                assert response2.status_code == 200

                # Third read should be throttled
                response3 = await client.get("/api/read")
                assert response3.status_code == 429

                # Write endpoint should have separate limit (different path)
                response4 = await client.post("/api/write")
                assert response4.status_code == 200

                # Second write should be throttled (limit=1)
                response5 = await client.post("/api/write")
                assert response5.status_code == 429

                # Verify read is still throttled
                response6 = await client.get("/api/read")
                assert response6.status_code == 429


@pytest.mark.anyio
@pytest.mark.throttle
async def test_multi_service_namespace_isolation(backends: BackendGen) -> None:
    """
    Test multiple services with similar backends using different namespaces for complete isolation.
    """
    for backend_a, backend_b in zip(
        backends(persistent=False, namespace="backend-a"),
        backends(persistent=False, namespace="backend-b"),
    ):
        async with backend_a(close_on_exit=True), backend_b(close_on_exit=True):
            # Service A
            throttle_a = HTTPThrottle(
                uid="api_limit",  # Same UID but different prefixes
                rate="2/s",
                identifier=default_client_identifier,
                backend=backend_a,
            )

            async def service_a_data(request):
                await throttle_a(request)
                return JSONResponse({"service": "A", "data": "response"})

            routes_a = [Route("/data", service_a_data, methods=["GET"])]
            app_a = Starlette(routes=routes_a)

            # Service B
            throttle_b = HTTPThrottle(
                uid="api_limit",  # Same UID but different prefixes
                rate="2/s",
                identifier=default_client_identifier,
                backend=backend_b,
            )

            async def service_b_data(request):
                await throttle_b(request)
                return JSONResponse({"service": "B", "data": "response"})

            routes_b = [Route("/data", service_b_data, methods=["GET"])]
            app_b = Starlette(routes=routes_b)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url=base_url
            ) as client_a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url=base_url
            ) as client_b:
                # Each service should have independent limits due to different prefixes

                # Exhaust service A's limit
                response1 = await client_a.get("/data")
                assert response1.status_code == 200

                response2 = await client_a.get("/data")
                assert response2.status_code == 200

                # Third request to service A should be throttled
                response3 = await client_a.get("/data")
                assert response3.status_code == 429

                # Service B should have its own independent limit
                response4 = await client_b.get("/data")
                assert response4.status_code == 200

                response5 = await client_b.get("/data")
                assert response5.status_code == 200

                # Third request to service B should be throttled
                response6 = await client_b.get("/data")
                assert response6.status_code == 429

                # Verify service A is still throttled
                response7 = await client_a.get("/data")
                assert response7.status_code == 429


@pytest.mark.anyio
@pytest.mark.throttle
async def test_multi_service_concurrency(backends: BackendGen) -> None:
    """
    Test concurrent access from multiple services to ensure consistency.
    """
    for backend in backends(
        persistent=False, namespace="multi_service_concurrency_test"
    ):
        async with backend(close_on_exit=True):
            # Shared configuration
            shared_config = {
                "uid": "concurrent_test",
                "rate": "6/min",
                "identifier": default_client_identifier,
            }

            # Service A
            throttle_a = HTTPThrottle(**shared_config)

            async def service_a_endpoint(request):
                await throttle_a(request)
                return JSONResponse({"service": "A"})

            routes_a = [Route("/endpoint", service_a_endpoint, methods=["GET"])]
            app_a = Starlette(routes=routes_a)

            # Service B
            throttle_b = HTTPThrottle(**shared_config)

            async def service_b_endpoint(request):
                await throttle_b(request)
                return JSONResponse({"service": "B"})

            routes_b = [Route("/endpoint", service_b_endpoint, methods=["GET"])]
            app_b = Starlette(routes=routes_b)

            # Service C
            throttle_c = HTTPThrottle(**shared_config)

            async def service_c_endpoint(request):
                await throttle_c(request)
                return JSONResponse({"service": "C"})

            routes_c = [Route("/endpoint", service_c_endpoint, methods=["GET"])]
            app_c = Starlette(routes=routes_c)

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url=base_url
            ) as client_a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url=base_url
            ) as client_b, AsyncClient(
                transport=ASGITransport(app=app_c), base_url=base_url
            ) as client_c:

                async def make_request(client: AsyncClient, service_name: str):
                    try:
                        response = await client.get("/endpoint")
                        return {"service": service_name, "status": response.status_code}
                    except Exception as exc:
                        return {
                            "service": service_name,
                            "status": "error",
                            "error": str(exc),
                        }

                # Make 10 concurrent requests across all services
                tasks = []
                for i in range(10):
                    if i % 3 == 0:
                        tasks.append(make_request(client_a, "A"))
                    elif i % 3 == 1:
                        tasks.append(make_request(client_b, "B"))
                    else:
                        tasks.append(make_request(client_c, "C"))

                results = await asyncio.gather(*tasks)

                # Count successful vs throttled responses
                success_count = sum(1 for r in results if r["status"] == 200)
                throttled_count = sum(1 for r in results if r["status"] == 429)

                # Should have exactly 6 successful requests (limit=6) and 4 throttled
                assert success_count == 6, (
                    f"Expected 6 successful requests, got {success_count}"
                )
                assert throttled_count == 4, (
                    f"Expected 4 throttled requests, got {throttled_count}"
                )

                # Verify responses came from all services
                services_used = {r["service"] for r in results if r["status"] == 200}
                assert len(services_used) >= 2, (
                    "Successful requests should come from multiple services"
                )
