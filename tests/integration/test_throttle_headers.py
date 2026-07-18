"""
Tests for header resolution and injection on HTTP throttles.

Runs against both FastAPI and Starlette via the `web_framework` fixture -- see
`tests/integration/_apps.py` for why this is safe: `await throttle(connection)`
and `await throttle.get_headers(connection)` behave identically on both stacks.

`backend=` is passed to each throttle explicitly rather than relying on the
app-lifespan/contextvar fallback, so these tests don't depend on the ASGI
transport running lifespan startup/shutdown.
"""

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.frameworks import ASGIFramework, HTTPRoute
from tests.utils import default_client_identifier, make_client
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import Header
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


@pytest.mark.throttle
@pytest.mark.anyio
class TestHTTPThrottleHeaders:
    async def test_static_string_headers(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Plain string headers are returned as-is without needing stat."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"static-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                headers={"X-Custom": "hello", "X-Service": "traffik"},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert resp.headers["X-Custom"] == "hello"
                assert resp.headers["X-Service"] == "traffik"

    async def test_dynamic_rate_limit_headers(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Default Header.LIMIT / REMAINING resolve from strategy stat."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"dynamic-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                headers={
                    "X-RateLimit-Limit": Header.LIMIT(when="always"),
                    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
                },
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp1 = await client.get("/api/data")
                assert resp1.status_code == 200
                assert resp1.headers["X-RateLimit-Limit"] == "5"
                assert resp1.headers["X-RateLimit-Remaining"] == "4"

                resp2 = await client.get("/api/data")
                assert resp2.status_code == 200
                assert resp2.headers["X-RateLimit-Remaining"] == "3"

    async def test_remaining_decrements_across_hits(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """X-RateLimit-Remaining should decrement with each hit."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"decrement-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                headers={
                    "X-RateLimit-Limit": Header.LIMIT(when="always"),
                    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
                },
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                for expected_remaining in range(4, -1, -1):
                    resp = await client.get("/api/data")
                    if resp.status_code == 429:
                        break
                    assert resp.headers["X-RateLimit-Limit"] == "5"
                    assert resp.headers["X-RateLimit-Remaining"] == str(
                        expected_remaining
                    )

    async def test_headers_only_on_throttled(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Header.when='throttled' fires once hits_remaining hits 0, not before."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"throttled-headers-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                headers={
                    "X-RateLimit-Reset": Header.RESET_SECONDS(when="throttled"),
                },
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # 1st hit: remaining = 2 - 1 = 1, not yet at the "throttled" floor.
                resp1 = await client.get("/api/data")
                assert resp1.status_code == 200
                assert "X-RateLimit-Reset" not in resp1.headers

                # 2nd hit: remaining = 2 - 2 = 0, `when="throttled"` (hits_remaining <= 0)
                # is satisfied even though the request itself is still allowed through.
                resp2 = await client.get("/api/data")
                assert resp2.status_code == 200
                assert "X-RateLimit-Reset" in resp2.headers

    async def test_header_disable_removes_header(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Header.DISABLE passed to get_headers() should remove a default header."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"disable-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                headers={
                    "X-RateLimit-Limit": Header.LIMIT(when="always"),
                    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
                    "X-Custom": "present",
                },
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(
                    request,
                    headers={"X-Custom": Header.DISABLE},
                )
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert "X-Custom" not in resp.headers
                assert "X-RateLimit-Limit" in resp.headers
                assert "X-RateLimit-Remaining" in resp.headers

    async def test_override_headers_in_get_headers(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Override headers passed to get_headers() take precedence."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"override-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                headers={"X-Version": "v1"},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(
                    request,
                    headers={"X-Version": "v2", "X-Extra": "bonus"},
                )
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert resp.headers["X-Version"] == "v2"
                assert resp.headers["X-Extra"] == "bonus"

    async def test_no_headers_returns_empty(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Throttle with no headers should return empty dict."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"no-headers-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True, "header_count": len(resolved)})

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert resp.json()["header_count"] == 0

    async def test_mixed_static_and_dynamic_headers(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Static and dynamic headers coexist correctly."""
        async with inmemory_backend(close_on_exit=True):
            throttle = HTTPThrottle(
                uid=f"mixed-headers-{web_framework.name}",
                rate="10/s",
                identifier=default_client_identifier,
                headers={
                    "X-Powered-By": "traffik",
                    "X-RateLimit-Limit": Header.LIMIT(when="always"),
                    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
                },
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                resolved = await throttle.get_headers(request)
                return JSONResponse({"ok": True}, headers=resolved)

            app = web_framework.build_app(http_routes=[HTTPRoute("/api/data", handler)])

            async with make_client(app, base_url="http://0.0.0.0") as client:
                resp = await client.get("/api/data")
                assert resp.status_code == 200
                assert resp.headers["X-Powered-By"] == "traffik"
                assert resp.headers["X-RateLimit-Limit"] == "10"
                assert resp.headers["X-RateLimit-Remaining"] == "9"
