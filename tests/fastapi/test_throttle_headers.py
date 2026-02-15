"""Integration tests for headers with throttles (FastAPI)."""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import Header
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


@pytest.mark.throttle
@pytest.mark.fastapi
def test_static_string_headers(inmemory_backend: InMemoryBackend) -> None:
    """Plain string headers are returned as-is without needing stat."""
    throttle = HTTPThrottle(
        uid="static-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        headers={"X-Custom": "hello", "X-Service": "traffik"},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers["X-Custom"] == "hello"
        assert resp.headers["X-Service"] == "traffik"


@pytest.mark.throttle
@pytest.mark.fastapi
def test_dynamic_rate_limit_headers(inmemory_backend: InMemoryBackend) -> None:
    """Default Header.LIMIT / REMAINING resolve from strategy stat."""
    throttle = HTTPThrottle(
        uid="dynamic-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        },
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp1 = client.get("/api/data")
        assert resp1.status_code == 200
        assert resp1.headers["X-RateLimit-Limit"] == "5"
        assert resp1.headers["X-RateLimit-Remaining"] == "4"

        resp2 = client.get("/api/data")
        assert resp2.status_code == 200
        assert resp2.headers["X-RateLimit-Remaining"] == "3"


@pytest.mark.throttle
@pytest.mark.fastapi
def test_header_disable_removes_header(inmemory_backend: InMemoryBackend) -> None:
    """Header.DISABLE passed to get_headers() should remove a default header."""
    throttle = HTTPThrottle(
        uid="disable-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
            "X-Custom": "present",
        },
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(
            request,
            headers={"X-Custom": Header.DISABLE},
        )
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert "X-Custom" not in resp.headers
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers


@pytest.mark.throttle
@pytest.mark.fastapi
def test_override_headers_in_get_headers(inmemory_backend: InMemoryBackend) -> None:
    """Override headers passed to get_headers() take precedence."""
    throttle = HTTPThrottle(
        uid="override-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        headers={"X-Version": "v1"},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(
            request,
            headers={"X-Version": "v2", "X-Extra": "bonus"},
        )
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers["X-Version"] == "v2"
        assert resp.headers["X-Extra"] == "bonus"


@pytest.mark.throttle
@pytest.mark.fastapi
def test_no_headers_returns_empty(inmemory_backend: InMemoryBackend) -> None:
    """Throttle with no headers should return empty dict."""
    throttle = HTTPThrottle(
        uid="no-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True, "header_count": len(resolved)})

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.json()["header_count"] == 0


@pytest.mark.throttle
@pytest.mark.fastapi
def test_mixed_static_and_dynamic_headers(inmemory_backend: InMemoryBackend) -> None:
    """Static and dynamic headers coexist correctly."""
    throttle = HTTPThrottle(
        uid="mixed-headers-fastapi",
        rate="10/s",
        identifier=default_client_identifier,
        headers={
            "X-Powered-By": "traffik",
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        },
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        resp = client.get("/api/data")
        assert resp.status_code == 200
        assert resp.headers["X-Powered-By"] == "traffik"
        assert resp.headers["X-RateLimit-Limit"] == "10"
        assert resp.headers["X-RateLimit-Remaining"] == "9"


@pytest.mark.throttle
@pytest.mark.fastapi
def test_remaining_decrements_across_hits(inmemory_backend: InMemoryBackend) -> None:
    """X-RateLimit-Remaining should decrement with each hit."""
    throttle = HTTPThrottle(
        uid="decrement-headers-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        headers={
            "X-RateLimit-Limit": Header.LIMIT(when="always"),
            "X-RateLimit-Remaining": Header.REMAINING(when="always"),
        },
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get("/api/data")
    async def handler(request: Request, _=Depends(throttle)) -> JSONResponse:
        resolved = await throttle.get_headers(request)
        return JSONResponse({"ok": True}, headers=resolved)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        for expected_remaining in range(4, -1, -1):
            resp = client.get("/api/data")
            if resp.status_code == 429:
                break
            assert resp.headers["X-RateLimit-Limit"] == "5"
            assert resp.headers["X-RateLimit-Remaining"] == str(expected_remaining)
