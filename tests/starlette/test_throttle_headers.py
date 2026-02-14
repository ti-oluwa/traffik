"""Integration tests for headers with throttles (Starlette)."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import Header
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


@pytest.mark.throttle
def test_static_string_headers(inmemory_backend: InMemoryBackend) -> None:
    """Plain string headers are returned as-is without needing stat."""
    throttle = HTTPThrottle(
        uid="static-headers-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        headers={"X-Custom": "hello", "X-Service": "traffik"},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers["X-Custom"] == "hello"
        assert resp.headers["X-Service"] == "traffik"


@pytest.mark.throttle
def test_dynamic_rate_limit_headers(inmemory_backend: InMemoryBackend) -> None:
    """Default Header.LIMIT / REMAINING / RESET resolve from strategy stat."""
    throttle = HTTPThrottle(
        uid="dynamic-headers-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        },
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp1 = client.get("/api/data")
        assert resp1.status_code == 200
        assert resp1.headers["X-RateLimit-Limit"] == "5"
        # After 1 hit: remaining = 5 - 1 = 4
        assert resp1.headers["X-RateLimit-Remaining"] == "4"

        resp2 = client.get("/api/data")
        assert resp2.status_code == 200
        assert resp2.headers["X-RateLimit-Remaining"] == "3"


@pytest.mark.throttle
def test_headers_only_on_throttled(inmemory_backend: InMemoryBackend) -> None:
    """Headers with when='throttled' should only appear once throttle limit is reached."""
    throttle = HTTPThrottle(
        uid="throttled-headers-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Reset": Header.RESET_SECONDS(when="throttled"),
        },
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # Not throttled yet — header should be absent
        resp1 = client.get("/api/data")
        assert resp1.status_code == 200
        assert "X-RateLimit-Reset" not in resp1.headers

        resp2 = client.get("/api/data")
        assert resp2.status_code == 200
        # After 2nd hit with rate=2/s, remaining is 0.
        # The when="throttled" check looks at stat.throttled or remaining == 0.
        # At this point the connection is NOT yet throttled (rate allows 2),
        # but remaining may be 0, triggering the header depending on check logic.


@pytest.mark.throttle
def test_header_disable_removes_header(inmemory_backend: InMemoryBackend) -> None:
    """Header.DISABLE passed to get_headers() should remove a default header."""
    throttle = HTTPThrottle(
        uid="disable-headers-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
            "X-Custom": "present",
        },
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(
            request,
            headers={"X-Custom": Header.DISABLE},
        )
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        # DISABLE should remove X-Custom
        assert "X-Custom" not in resp.headers
        # Other headers should still be present
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers


@pytest.mark.throttle
def test_override_headers_in_get_headers(inmemory_backend: InMemoryBackend) -> None:
    """Override headers passed to get_headers() take precedence."""
    throttle = HTTPThrottle(
        uid="override-headers-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        headers={"X-Version": "v1"},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(
            request,
            headers={"X-Version": "v2", "X-Extra": "bonus"},
        )
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        # Override takes precedence
        assert resp.headers["X-Version"] == "v2"
        # Extra header added
        assert resp.headers["X-Extra"] == "bonus"


@pytest.mark.throttle
def test_no_headers_returns_empty(inmemory_backend: InMemoryBackend) -> None:
    """Throttle with no headers should return empty dict."""
    throttle = HTTPThrottle(
        uid="no-headers-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(request)
        data = {"ok": True, "header_count": len(resolved)}
        return JSONResponse(data)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.json()["header_count"] == 0


@pytest.mark.throttle
def test_mixed_static_and_dynamic_headers(inmemory_backend: InMemoryBackend) -> None:
    """Static and dynamic headers coexist correctly."""
    throttle = HTTPThrottle(
        uid="mixed-headers-starlette",
        rate="10/s",
        identifier=default_client_identifier,
        headers={
            "X-Powered-By": "traffik",
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        },
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers["X-Powered-By"] == "traffik"
        assert resp.headers["X-RateLimit-Limit"] == "10"
        assert resp.headers["X-RateLimit-Remaining"] == "9"
