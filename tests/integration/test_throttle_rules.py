"""
Tests for `BypassThrottleRule`/`ThrottleRule` gating and bypass behavior.
"""

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.frameworks import ASGIFramework, HTTPRoute
from tests.utils import default_client_identifier, make_client
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import BypassThrottleRule, ThrottleRegistry, ThrottleRule
from traffik.throttles import HTTPThrottle


@pytest.mark.registry
@pytest.mark.throttle
@pytest.mark.anyio
class TestThrottleRules:
    async def test_bypass_rule_skips_throttle_for_matching_path(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Requests matching a BypassThrottleRule should never be throttled."""
        async with inmemory_backend(close_on_exit=True):
            bypass = BypassThrottleRule(path="/api/users", methods={"GET"})
            throttle = HTTPThrottle(
                uid=f"bypass-ctor-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                rules={bypass},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/users", handler, methods=["GET"]),
                    HTTPRoute("/api/data", handler, methods=["GET"]),
                ]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # GET /api/users - bypassed, never throttled
                for _ in range(5):
                    assert (await client.get("/api/users")).status_code == 200

                # GET /api/data - not bypassed, throttled after 2
                assert (await client.get("/api/data")).status_code == 200
                assert (await client.get("/api/data")).status_code == 200
                assert (await client.get("/api/data")).status_code == 429

    async def test_bypass_rule_method_specific(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Bypass only matches the specified method; other methods still throttle."""
        async with inmemory_backend(close_on_exit=True):
            bypass = BypassThrottleRule(path="/api/items", methods={"GET"})
            throttle = HTTPThrottle(
                uid=f"bypass-method-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                rules={bypass},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/items", handler, methods=["GET", "POST"]),
                ]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # GET is bypassed - unlimited
                for _ in range(5):
                    assert (await client.get("/api/items")).status_code == 200

                # POST is NOT bypassed - throttled after 2
                assert (await client.post("/api/items")).status_code == 200
                assert (await client.post("/api/items")).status_code == 200
                assert (await client.post("/api/items")).status_code == 429

    async def test_gating_rule_only_throttles_matching_requests(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """ThrottleRule gates the throttle - only matching requests consume quota."""
        async with inmemory_backend(close_on_exit=True):
            gate = ThrottleRule(methods={"POST"})
            throttle = HTTPThrottle(
                uid=f"gate-ctor-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                rules={gate},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/data", handler, methods=["GET", "POST"]),
                ]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # GET doesn't match the gate -> throttle skipped -> unlimited
                for _ in range(5):
                    assert (await client.get("/api/data")).status_code == 200

                # POST matches the gate -> throttle applies -> 429 after 2
                assert (await client.post("/api/data")).status_code == 200
                assert (await client.post("/api/data")).status_code == 200
                assert (await client.post("/api/data")).status_code == 429

    async def test_add_rules_bypass_on_parent_throttle(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """
        A sub-throttle adds a BypassThrottleRule to a parent/global throttle so
        specific routes skip the global limit.
        """
        async with inmemory_backend(close_on_exit=True):
            registry = ThrottleRegistry()
            global_uid = f"global-{web_framework.name}"

            global_throttle = HTTPThrottle(
                uid=global_uid,
                rate="2/s",
                identifier=default_client_identifier,
                registry=registry,
                backend=inmemory_backend,
            )
            sub_throttle = HTTPThrottle(
                uid=f"sub-{web_framework.name}",
                rate="5/s",
                identifier=default_client_identifier,
                registry=registry,
                backend=inmemory_backend,
            )

            # Sub-throttle bypasses global for GET /api/v1/users
            bypass = BypassThrottleRule(path="/api/v1/users", methods={"GET"})
            sub_throttle.add_rules(global_uid, bypass)

            async def users_handler(request: Request) -> JSONResponse:
                await global_throttle(request)
                await sub_throttle(request)
                return JSONResponse({"ok": True})

            async def data_handler(request: Request) -> JSONResponse:
                await global_throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/v1/users", users_handler, methods=["GET"]),
                    HTTPRoute("/api/v1/data", data_handler, methods=["GET"]),
                ]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # GET /api/v1/users: global is bypassed, only sub-throttle (5/s) applies
                for _ in range(5):
                    assert (await client.get("/api/v1/users")).status_code == 200
                assert (await client.get("/api/v1/users")).status_code == 429

                # GET /api/v1/data: global applies (0 consumed so far, users bypassed it)
                assert (await client.get("/api/v1/data")).status_code == 200
                assert (await client.get("/api/v1/data")).status_code == 200
                assert (await client.get("/api/v1/data")).status_code == 429

    async def test_multiple_bypass_rules(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Multiple bypass rules each independently skip the throttle."""
        async with inmemory_backend(close_on_exit=True):
            bypass_get_users = BypassThrottleRule(path="/api/users", methods={"GET"})
            bypass_post_orgs = BypassThrottleRule(path="/api/orgs", methods={"POST"})

            throttle = HTTPThrottle(
                uid=f"multi-bypass-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                rules={bypass_get_users, bypass_post_orgs},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[
                    HTTPRoute("/api/users", handler, methods=["GET"]),
                    HTTPRoute("/api/orgs", handler, methods=["POST"]),
                    HTTPRoute("/api/other", handler, methods=["GET"]),
                ]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # Both bypassed paths should be unlimited
                for _ in range(5):
                    assert (await client.get("/api/users")).status_code == 200
                for _ in range(5):
                    assert (await client.post("/api/orgs")).status_code == 200

                # Non-bypassed path throttles after 2
                assert (await client.get("/api/other")).status_code == 200
                assert (await client.get("/api/other")).status_code == 200
                assert (await client.get("/api/other")).status_code == 429

    async def test_bypass_rule_with_predicate(
        self, inmemory_backend: InMemoryBackend, web_framework: ASGIFramework
    ) -> None:
        """Bypass rule with a predicate: only bypass when the predicate matches."""
        async with inmemory_backend(close_on_exit=True):

            async def is_internal(conn: Request) -> bool:
                return conn.headers.get("x-internal") == "true"

            bypass = BypassThrottleRule(predicate=is_internal)
            throttle = HTTPThrottle(
                uid=f"bypass-pred-{web_framework.name}",
                rate="2/s",
                identifier=default_client_identifier,
                rules={bypass},
                registry=ThrottleRegistry(),
                backend=inmemory_backend,
            )

            async def handler(request: Request) -> JSONResponse:
                await throttle(request)
                return JSONResponse({"ok": True})

            app = web_framework.build_app(
                http_routes=[HTTPRoute("/api/data", handler, methods=["GET"])]
            )

            async with make_client(app, base_url="http://0.0.0.0") as client:
                # Internal requests bypass -> unlimited
                for _ in range(5):
                    resp = await client.get("/api/data", headers={"x-internal": "true"})
                    assert resp.status_code == 200

                # External requests -> throttled after 2
                assert (await client.get("/api/data")).status_code == 200
                assert (await client.get("/api/data")).status_code == 200
                assert (await client.get("/api/data")).status_code == 429
