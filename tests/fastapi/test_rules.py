"""Integration tests for throttle rules with FastAPI."""

import typing

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.utils import default_client_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.registry import BypassThrottleRule, ThrottleRegistry, ThrottleRule
from traffik.throttles import HTTPThrottle


@pytest.mark.throttle
@pytest.mark.fastapi
def test_bypass_rule_skips_throttle_for_matching_path(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Requests matching a BypassThrottleRule should never be throttled."""
    bypass = BypassThrottleRule(path="/api/users", methods={"GET"})
    throttle = HTTPThrottle(
        uid="bypass-ctor-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    router = APIRouter(prefix="/api", dependencies=[Depends(throttle)])

    @router.get("/users")
    async def get_users() -> typing.Dict[str, bool]:
        return {"ok": True}

    @router.get("/data")
    async def get_data() -> typing.Dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET /api/users — bypassed, never throttled
        for _ in range(5):
            assert client.get("/api/users").status_code == 200

        # GET /api/data — not bypassed, throttled after 2
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429


@pytest.mark.throttle
@pytest.mark.fastapi
def test_bypass_rule_method_specific(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Bypass only matches the specified method; other methods still throttle."""
    bypass = BypassThrottleRule(path="/api/items", methods={"GET"})
    throttle = HTTPThrottle(
        uid="bypass-method-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    router = APIRouter(prefix="/api", dependencies=[Depends(throttle)])

    @router.get("/items")
    async def get_items() -> typing.Dict[str, bool]:
        return {"ok": True}

    @router.post("/items")
    async def create_item() -> typing.Dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET is bypassed — unlimited
        for _ in range(5):
            assert client.get("/api/items").status_code == 200

        # POST is NOT bypassed — throttled after 2
        assert client.post("/api/items").status_code == 200
        assert client.post("/api/items").status_code == 200
        assert client.post("/api/items").status_code == 429


@pytest.mark.throttle
@pytest.mark.fastapi
def test_gating_rule_only_throttles_matching_requests(
    inmemory_backend: InMemoryBackend,
) -> None:
    """ThrottleRule gates the throttle — only matching requests consume quota."""
    gate = ThrottleRule(methods={"POST"})
    throttle = HTTPThrottle(
        uid="gate-ctor-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        rules={gate},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    router = APIRouter(prefix="/api", dependencies=[Depends(throttle)])

    @router.get("/data")
    async def get_data() -> typing.Dict[str, bool]:
        return {"ok": True}

    @router.post("/data")
    async def post_data() -> typing.Dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET doesn't match the gate → throttle skipped → unlimited
        for _ in range(5):
            assert client.get("/api/data").status_code == 200

        # POST matches the gate → throttle applies → 429 after 2
        assert client.post("/api/data").status_code == 200
        assert client.post("/api/data").status_code == 200
        assert client.post("/api/data").status_code == 429


@pytest.mark.throttle
@pytest.mark.fastapi
def test_add_rules_bypass_on_parent_throttle(
    inmemory_backend: InMemoryBackend,
) -> None:
    """
    Example B pattern: sub-throttle adds a BypassThrottleRule to the
    global/parent throttle so specific sub-routes skip the global limit.
    """
    registry = ThrottleRegistry()

    global_throttle = HTTPThrottle(
        uid="global-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        registry=registry,
    )
    sub_throttle = HTTPThrottle(
        uid="sub-fastapi",
        rate="5/s",
        identifier=default_client_identifier,
        registry=registry,
    )

    # Sub-throttle bypasses global for GET /api/v1/users
    bypass = BypassThrottleRule(path="/api/v1/users", methods={"GET"})
    sub_throttle.add_rules("global-fastapi", bypass)

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    main_router = APIRouter(prefix="/api/v1", dependencies=[Depends(global_throttle)])
    users_router = APIRouter(prefix="/users", dependencies=[Depends(sub_throttle)])

    @users_router.get("")
    async def get_users() -> typing.Dict[str, bool]:
        return {"ok": True}

    @main_router.get("/data")
    async def get_data() -> typing.Dict[str, bool]:
        return {"ok": True}

    main_router.include_router(users_router)
    app.include_router(main_router)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # GET /api/v1/users: global is bypassed, only sub-throttle (5/s)
        for _ in range(5):
            assert client.get("/api/v1/users").status_code == 200
        assert client.get("/api/v1/users").status_code == 429

        # GET /api/v1/data: global applies (0 consumed so far since users bypassed it)
        assert client.get("/api/v1/data").status_code == 200
        assert client.get("/api/v1/data").status_code == 200
        assert client.get("/api/v1/data").status_code == 429


@pytest.mark.throttle
@pytest.mark.fastapi
def test_multiple_bypass_rules_constructor(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Multiple bypass rules each independently skip the throttle."""
    bypass_get_users = BypassThrottleRule(path="/api/users", methods={"GET"})
    bypass_post_orgs = BypassThrottleRule(path="/api/orgs", methods={"POST"})

    throttle = HTTPThrottle(
        uid="multi-bypass-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass_get_users, bypass_post_orgs},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)
    router = APIRouter(prefix="/api", dependencies=[Depends(throttle)])

    @router.get("/users")
    async def get_users() -> typing.Dict[str, bool]:
        return {"ok": True}

    @router.post("/orgs")
    async def create_org() -> typing.Dict[str, bool]:
        return {"ok": True}

    @router.get("/other")
    async def get_other() -> typing.Dict[str, bool]:
        return {"ok": True}

    app.include_router(router)

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # Both bypassed paths should be unlimited
        for _ in range(5):
            assert client.get("/api/users").status_code == 200
        for _ in range(5):
            assert client.post("/api/orgs").status_code == 200

        # Non-bypassed path throttles after 2
        assert client.get("/api/other").status_code == 200
        assert client.get("/api/other").status_code == 200
        assert client.get("/api/other").status_code == 429


@pytest.mark.throttle
@pytest.mark.fastapi
def test_bypass_rule_with_predicate(
    inmemory_backend: InMemoryBackend,
) -> None:
    """Bypass with a predicate: only bypass when predicate matches."""

    async def is_internal(conn: Request) -> bool:
        return conn.headers.get("x-internal") == "true"

    bypass = BypassThrottleRule(predicate=is_internal)
    throttle = HTTPThrottle(
        uid="bypass-pred-fastapi",
        rate="2/s",
        identifier=default_client_identifier,
        rules={bypass},
        registry=ThrottleRegistry(),
    )

    app = FastAPI(lifespan=inmemory_backend.lifespan)

    @app.get(
        "/api/data",
        dependencies=[Depends(throttle)],
    )
    async def get_data() -> typing.Dict[str, bool]:
        return {"ok": True}

    with TestClient(app, base_url="http://0.0.0.0") as client:
        # Internal requests bypass → unlimited
        for _ in range(5):
            resp = client.get("/api/data", headers={"x-internal": "true"})
            assert resp.status_code == 200

        # External requests → throttled after 2
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429
