"""Integration tests for throttle rules with Starlette."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import BypassThrottleRule, ThrottleRegistry, ThrottleRule
from traffik.throttles import HTTPThrottle


@pytest.mark.throttle
def test_bypass_rule_skips_throttle_for_matching_path(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Requests matching a BypassThrottleRule should never be throttled."""
    bypass = BypassThrottleRule(path="/api/users", methods={"GET"})
    throttle = HTTPThrottle(
        uid="bypass-ctor-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"ok": True})

    routes = [
        Route("/api/users", handler, methods=["GET", "POST"]),
        Route("/api/data", handler, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET /api/users — bypassed, should never be throttled
        for _ in range(5):
            assert client.get("/api/users").status_code == 200

        # GET /api/data — not bypassed, should throttle after 2
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429


@pytest.mark.throttle
def test_bypass_rule_method_specific(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Bypass only matches the specified method; other methods are still throttled."""
    bypass = BypassThrottleRule(path="/api/items", methods={"GET"})
    throttle = HTTPThrottle(
        uid="bypass-method-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"ok": True})

    routes = [Route("/api/items", handler, methods=["GET", "POST"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET is bypassed — unlimited
        for _ in range(5):
            assert client.get("/api/items").status_code == 200

        # POST is NOT bypassed — throttled after 2
        assert client.post("/api/items").status_code == 200
        assert client.post("/api/items").status_code == 200
        assert client.post("/api/items").status_code == 429


@pytest.mark.throttle
def test_gating_rule_only_throttles_matching_requests(
    inmemory_backend: InMemoryBackend,
) -> None:
    """ThrottleRule gates the throttle — only matching requests consume quota."""
    gate = ThrottleRule(methods={"POST"})
    throttle = HTTPThrottle(
        uid="gate-ctor-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        rules={gate},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"ok": True})

    routes = [Route("/api/data", handler, methods=["GET", "POST"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET doesn't match the gate → throttle skipped → unlimited
        for _ in range(5):
            assert client.get("/api/data").status_code == 200

        # POST matches the gate → throttle applies → 429 after 2
        assert client.post("/api/data").status_code == 200
        assert client.post("/api/data").status_code == 200
        assert client.post("/api/data").status_code == 429


@pytest.mark.throttle
def test_add_rules_bypass_on_another_throttle(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Pattern from example B: a sub-throttle adds a BypassThrottleRule to a
    parent/global throttle so specific routes skip the global limit.
    """
    registry = ThrottleRegistry()

    global_throttle = HTTPThrottle(
        uid="global-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        registry=registry,
    )
    sub_throttle = HTTPThrottle(
        uid="sub-starlette",
        rate="5/s",
        identifier=default_client_identifier,
        registry=registry,
    )

    # Sub-throttle bypasses global for GET /api/users
    bypass = BypassThrottleRule(path="/api/users", methods={"GET"})
    sub_throttle.add_rules("global-starlette", bypass)

    async def users_handler(request: Request) -> JSONResponse:
        await global_throttle(request)
        await sub_throttle(request)
        return JSONResponse({"ok": True})

    async def data_handler(request: Request) -> JSONResponse:
        await global_throttle(request)
        return JSONResponse({"ok": True})

    routes = [
        Route("/api/users", users_handler, methods=["GET"]),
        Route("/api/data", data_handler, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET /api/users: global is bypassed, only sub-throttle applies (5/s)
        for _ in range(5):
            assert client.get("/api/users").status_code == 200
        assert client.get("/api/users").status_code == 429

        # GET /api/data: global applies (limit was 2, but 0 consumed since users bypassed it)
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429


@pytest.mark.throttle
def test_multiple_bypass_rules(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Multiple bypass rules each independently skip the throttle."""
    bypass_get_users = BypassThrottleRule(path="/api/users", methods={"GET"})
    bypass_post_orgs = BypassThrottleRule(path="/api/orgs", methods={"POST"})

    throttle = HTTPThrottle(
        uid="multi-bypass-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass_get_users, bypass_post_orgs},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"ok": True})

    routes = [
        Route("/api/users", handler, methods=["GET"]),
        Route("/api/orgs", handler, methods=["POST"]),
        Route("/api/other", handler, methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # Both bypassed paths should be unlimited
        for _ in range(5):
            assert client.get("/api/users").status_code == 200
        for _ in range(5):
            assert client.post("/api/orgs").status_code == 200

        # Non-bypassed path should throttle after 2
        assert client.get("/api/other").status_code == 200
        assert client.get("/api/other").status_code == 200
        assert client.get("/api/other").status_code == 429


@pytest.mark.throttle
def test_bypass_rule_with_predicate(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Bypass rule with a predicate: only bypass when predicate returns True."""

    async def is_internal(conn: Request) -> bool:
        return conn.headers.get("x-internal") == "true"

    bypass = BypassThrottleRule(predicate=is_internal)
    throttle = HTTPThrottle(
        uid="bypass-pred-starlette",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    async def handler(request: Request) -> JSONResponse:
        await throttle(request)
        return JSONResponse({"ok": True})

    routes = [Route("/api/data", handler, methods=["GET"])]
    app = Starlette(routes=routes, lifespan=inmemory_backend.lifespan)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # Internal requests bypass → unlimited
        for _ in range(5):
            resp = client.get("/api/data", headers={"x-internal": "true"})
            assert resp.status_code == 200

        # External requests → throttled after 2
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429
