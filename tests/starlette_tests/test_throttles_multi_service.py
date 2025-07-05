import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Route

from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend
from traffik.throttles import HTTPThrottle
from traffik.types import UNLIMITED

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def redis_backend() -> RedisBackend:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return RedisBackend(connection=redis, prefix="redis-test", persistent=False)


async def _testclient_identifier(connection: HTTPConnection) -> str:
    return "testclient"


async def _unlimited_identifier(connection: HTTPConnection) -> object:
    return UNLIMITED


###########################################################
# Multi-service tests for HTTPThrottle with Redis backend #
###########################################################


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_shared_redis_backend(redis_backend: RedisBackend) -> None:
    """
    Test multiple Starlette services sharing the same Redis backend.
    Throttles with same UID AND same path should share limits across services.
    """
    # Shared throttle configuration across services
    shared_throttle_config = {
        "uid": "shared_api_limit",
        "limit": 3,
        "milliseconds": 200,
        "identifier": _testclient_identifier,
    }

    # Service A
    async with redis_backend():
        throttle_a = HTTPThrottle(**shared_throttle_config)

        async def service_a_endpoint(request):
            await throttle_a(request)
            return JSONResponse({"service": "A", "data": "response"})

        routes_a = [Route("/api/shared", service_a_endpoint, methods=["GET"])]
        app_a = Starlette(routes=routes_a)

    # Service B
    async with redis_backend():
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
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_different_paths_redis_backend(
    redis_backend: RedisBackend,
) -> None:
    """
    Test that services with same UID but different paths have separate limits.
    This demonstrates that path is part of the key generation.
    """
    # Same UID but different endpoints
    shared_uid_config = {
        "uid": "same_uid_different_paths",
        "limit": 2,
        "milliseconds": 200,
        "identifier": _testclient_identifier,
    }

    # Service A
    async with redis_backend():
        throttle_a = HTTPThrottle(**shared_uid_config)

        async def service_a_endpoint(request):
            await throttle_a(request)
            return JSONResponse({"service": "A", "data": "response"})

        routes_a = [Route("/service-a/data", service_a_endpoint, methods=["GET"])]
        app_a = Starlette(routes=routes_a)

    # Service B
    async with redis_backend():
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
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_microservices_pattern_redis(
    redis_backend: RedisBackend,
) -> None:
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

    # User Service
    async with redis_backend():
        user_throttle = HTTPThrottle(
            uid="tenant_api_operations",
            limit=3,
            milliseconds=200,
            identifier=tenant_identifier,
        )

        async def get_user_operations(request):
            await user_throttle(request)
            return JSONResponse({"service": "user", "operation": "data_access"})

        user_routes = [Route("/api/operations", get_user_operations, methods=["GET"])]
        user_app = Starlette(routes=user_routes)

    # Order Service
    async with redis_backend():
        order_throttle = HTTPThrottle(
            uid="tenant_api_operations",  # Same UID
            limit=3,
            milliseconds=200,
            identifier=tenant_identifier,
        )

        async def get_order_operations(request):
            await order_throttle(request)
            return JSONResponse({"service": "order", "operation": "data_access"})

        order_routes = [Route("/api/operations", get_order_operations, methods=["GET"])]
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
        response1 = await user_client.get("/api/operations", headers=tenant_a_headers)
        assert response1.status_code == 200

        response2 = await order_client.get("/api/operations", headers=tenant_a_headers)
        assert response2.status_code == 200

        response3 = await user_client.get("/api/operations", headers=tenant_a_headers)
        assert response3.status_code == 200

        # Fourth request for tenant A should be throttled (shared limit=3)
        response4 = await order_client.get("/api/operations", headers=tenant_a_headers)
        assert response4.status_code == 429

        # Tenant B should have separate limits (different identifier)
        response5 = await user_client.get("/api/operations", headers=tenant_b_headers)
        assert response5.status_code == 200

        response6 = await order_client.get("/api/operations", headers=tenant_b_headers)
        assert response6.status_code == 200

        response7 = await user_client.get("/api/operations", headers=tenant_b_headers)
        assert response7.status_code == 200

        # Fourth request for tenant B should be throttled
        response8 = await order_client.get("/api/operations", headers=tenant_b_headers)
        assert response8.status_code == 429


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_path_specific_limits_redis() -> None:
    """
    Test that different endpoints in the same service have separate limits
    even with the same UID (due to different paths).
    """
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    backend = RedisBackend(connection=redis, prefix="redis-test", persistent=False)

    async with backend():
        # Same UID for both endpoints
        shared_uid = "endpoint_specific_limits"

        read_throttle = HTTPThrottle(
            uid=shared_uid,
            limit=2,
            milliseconds=200,
            identifier=_testclient_identifier,
        )

        write_throttle = HTTPThrottle(
            uid=shared_uid,  # Same UID
            limit=1,  # Different limit to test isolation
            milliseconds=200,
            identifier=_testclient_identifier,
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

    await backend.close()


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_prefix_isolation_redis() -> None:
    """
    Test multiple services using different Redis prefixes for complete isolation.
    """
    # Create separate Redis backends with different prefixes
    redis_service_a = Redis.from_url(REDIS_URL, decode_responses=True)
    redis_service_b = Redis.from_url(REDIS_URL, decode_responses=True)

    backend_a = RedisBackend(
        connection=redis_service_a, prefix="service-a", persistent=False
    )
    backend_b = RedisBackend(
        connection=redis_service_b, prefix="service-b", persistent=False
    )

    # Service A
    async with backend_a():
        throttle_a = HTTPThrottle(
            uid="api_limit",  # Same UID but different prefixes
            limit=2,
            milliseconds=200,
            identifier=_testclient_identifier,
        )

        async def service_a_data(request):
            await throttle_a(request)
            return JSONResponse({"service": "A", "data": "response"})

        routes_a = [Route("/data", service_a_data, methods=["GET"])]
        app_a = Starlette(routes=routes_a)

    # Service B
    async with backend_b():
        throttle_b = HTTPThrottle(
            uid="api_limit",  # Same UID but different prefixes
            limit=2,
            milliseconds=200,
            identifier=_testclient_identifier,
        )

        async def service_b_data(request):
            await throttle_b(request)
            return JSONResponse({"service": "B", "data": "response"})

        routes_b = [Route("/data", service_b_data, methods=["GET"])]
        app_b = Starlette(routes=routes_b)

    base_url = "http://0.0.0.0"

    try:
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

    finally:
        # Cleanup
        await backend_a.close()
        await backend_b.close()


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.throttle
@pytest.mark.redis
@pytest.mark.native
async def test_multi_service_concurrent_access_redis(
    redis_backend: RedisBackend,
) -> None:
    """
    Test concurrent access from multiple services to ensure thread safety.
    """
    # Shared configuration
    shared_config = {
        "uid": "concurrent_test",
        "limit": 5,
        "milliseconds": 500,
        "identifier": _testclient_identifier,
    }

    # Service A
    async with redis_backend():
        throttle_a = HTTPThrottle(**shared_config)

        async def service_a_endpoint(request):
            await throttle_a(request)
            return JSONResponse({"service": "A"})

        routes_a = [Route("/endpoint", service_a_endpoint, methods=["GET"])]
        app_a = Starlette(routes=routes_a)

    # Service B
    async with redis_backend():
        throttle_b = HTTPThrottle(**shared_config)

        async def service_b_endpoint(request):
            await throttle_b(request)
            return JSONResponse({"service": "B"})

        routes_b = [Route("/endpoint", service_b_endpoint, methods=["GET"])]
        app_b = Starlette(routes=routes_b)

    # Service C
    async with redis_backend():
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

        async def make_request(client, service_name):
            try:
                response = await client.get("/endpoint")
                return {"service": service_name, "status": response.status_code}
            except Exception as e:
                return {"service": service_name, "status": "error", "error": str(e)}

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

        # Should have exactly 5 successful requests (limit=5) and 5 throttled
        assert success_count == 5, (
            f"Expected 5 successful requests, got {success_count}"
        )
        assert throttled_count == 5, (
            f"Expected 5 throttled requests, got {throttled_count}"
        )

        # Verify responses came from all services
        services_used = {r["service"] for r in results if r["status"] == 200}
        assert len(services_used) >= 2, (
            "Successful requests should come from multiple services"
        )
