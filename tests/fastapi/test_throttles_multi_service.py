import asyncio
import typing

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import HTTPConnection

from tests.conftest import BackendGen
from tests.utils import default_client_identifier
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.fastapi
async def test_multi_service_shared_backend(backends: BackendGen) -> None:
    """
    Test multiple FastAPI services sharing the same backend.
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
            app_a = FastAPI()
            throttle_a = HTTPThrottle(**shared_throttle_config)

            @app_a.get("/api/shared", dependencies=[Depends(throttle_a)])
            async def service_a_endpoint() -> typing.Dict[str, str]:
                return {"service": "A", "data": "response"}

            # Service B
            app_b = FastAPI()
            throttle_b = HTTPThrottle(**shared_throttle_config)

            @app_b.get("/api/shared", dependencies=[Depends(throttle_b)])
            async def service_b_endpoint() -> typing.Dict[str, str]:
                return {"service": "B", "data": "response"}

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
@pytest.mark.fastapi
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
            app_a = FastAPI()
            throttle_a = HTTPThrottle(**shared_uid_config)

            @app_a.get("/service-a/data", dependencies=[Depends(throttle_a)])
            async def service_a_endpoint() -> typing.Dict[str, str]:
                return {"service": "A", "data": "response"}

            # Service B
            app_b = FastAPI()
            throttle_b = HTTPThrottle(**shared_uid_config)

            @app_b.get("/service-b/data", dependencies=[Depends(throttle_b)])
            async def service_b_endpoint() -> typing.Dict[str, str]:
                return {"service": "B", "data": "response"}

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
@pytest.mark.fastapi
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
            app_user = FastAPI()
            user_throttle = HTTPThrottle(
                uid="tenant_api_operations",
                rate="3/min",
                identifier=tenant_identifier,
            )

            @app_user.get("/api/operations", dependencies=[Depends(user_throttle)])
            async def get_user_operations() -> typing.Dict[str, str]:
                return {"service": "user", "operation": "data_access"}

            # Order Service
            app_order = FastAPI()
            order_throttle = HTTPThrottle(
                uid="tenant_api_operations",  # Same UID
                rate="3/min",
                identifier=tenant_identifier,
            )

            @app_order.get("/api/operations", dependencies=[Depends(order_throttle)])
            async def get_order_operations() -> typing.Dict[str, str]:
                return {"service": "order", "operation": "data_access"}

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_user), base_url=base_url
            ) as user_client, AsyncClient(
                transport=ASGITransport(app=app_order), base_url=base_url
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
@pytest.mark.fastapi
async def test_multi_service_endpoint_isolation(backends: BackendGen) -> None:
    """
    Test that different endpoints in the same service that have separate limits
    are isolated, even with the same UID (due to different paths).
    """
    for backend in backends(persistent=False, namespace="endpoint_isolation_test"):
        async with backend(close_on_exit=True):
            app = FastAPI()

            # Same UID for both endpoints
            shared_uid = "endpoint_specific_limits"
            read_throttle = HTTPThrottle(
                uid=shared_uid,
                rate="2/min",
                identifier=default_client_identifier,
            )
            write_throttle = HTTPThrottle(
                uid=shared_uid,  # Same UID
                # Different limit to test isolation
                rate="1/min",
                identifier=default_client_identifier,
            )

            @app.get("/api/read", dependencies=[Depends(read_throttle)])
            async def read_endpoint() -> typing.Dict[str, str]:
                return {"operation": "read", "status": "success"}

            @app.post("/api/write", dependencies=[Depends(write_throttle)])
            async def write_endpoint() -> typing.Dict[str, str]:
                return {"operation": "write", "status": "success"}

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
@pytest.mark.fastapi
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
            app_a = FastAPI()
            throttle_a = HTTPThrottle(
                uid="api_limit",  # Same UID but different namespaces
                rate="2/s",
                identifier=default_client_identifier,
                backend=backend_a,
            )

            @app_a.get("/data", dependencies=[Depends(throttle_a)])
            async def service_a_data() -> typing.Dict[str, str]:
                return {"service": "A", "data": "response"}

            # Service B
            app_b = FastAPI()
            throttle_b = HTTPThrottle(
                uid="api_limit",  # Same UID but different namespaces
                rate="2/s",
                identifier=default_client_identifier,
                backend=backend_b,
            )

            @app_b.get("/data", dependencies=[Depends(throttle_b)])
            async def service_b_data() -> typing.Dict[str, str]:
                return {"service": "B", "data": "response"}

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app_a), base_url=base_url
            ) as client_a, AsyncClient(
                transport=ASGITransport(app=app_b), base_url=base_url
            ) as client_b:
                # Each service should have independent limits due to different namespaces

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
@pytest.mark.fastapi
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
            app_a = FastAPI()
            throttle_a = HTTPThrottle(**shared_config)

            @app_a.get("/endpoint", dependencies=[Depends(throttle_a)])
            async def service_a_endpoint() -> typing.Dict[str, str]:
                return {"service": "A"}

            # Service B
            app_b = FastAPI()
            throttle_b = HTTPThrottle(**shared_config)

            @app_b.get("/endpoint", dependencies=[Depends(throttle_b)])
            async def service_b_endpoint() -> typing.Dict[str, str]:
                return {"service": "B"}

            # Service C
            app_c = FastAPI()
            throttle_c = HTTPThrottle(**shared_config)

            @app_c.get("/endpoint", dependencies=[Depends(throttle_c)])
            async def service_c_endpoint() -> typing.Dict[str, str]:
                return {"service": "C"}

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
