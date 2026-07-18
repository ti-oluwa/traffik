"""
FastAPI-only throttle regression tests.

Everything that doesn't actually depend on FastAPI's dependency-injection system
(`Depends()`, OpenAPI schema generation) now lives in
`tests/integration/test_throttles.py`, parametrized across both frameworks via
the `web_framework` fixture. What's left here specifically exercises the
`Depends(throttle)` / OpenAPI interaction, which Starlette has no equivalent of.
"""

import typing

import pytest
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from tests.utils import default_client_identifier, make_client, unlimited_identifier
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle


@pytest.fixture(scope="function")
def app(inmemory_backend: InMemoryBackend) -> FastAPI:
    """Lifespan fixture for FastAPI app to ensure proper startup and shutdown."""
    return FastAPI(lifespan=inmemory_backend.lifespan)


class ItemModel(BaseModel):
    name: str
    price: float


@pytest.mark.throttle
@pytest.mark.fastapi
@pytest.mark.anyio
class TestThrottlesDepedencyInjection:
    async def test_throttle_with_app_lifespan(self, app: FastAPI) -> None:
        throttle = HTTPThrottle(
            "test-throttle-app-lifespan-fa",
            rate="2/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.get("/", dependencies=[Depends(throttle)], status_code=200)
        async def ping_endpoint() -> typing.Dict[str, str]:
            return {"message": "PONG"}

        async with make_client(app, base_url="http://0.0.0.0") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.json() == {"message": "PONG"}

            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 429
            assert response.headers["Retry-After"] is not None

    async def test_throttle_exemption_with_unlimited_identifier(
        self,
        inmemory_backend: InMemoryBackend,
    ) -> None:
        """Same as the generic exemption test, but via `Depends(throttle)`."""
        throttle = HTTPThrottle(
            "test-throttle-exemption-fa",
            rate="2/s",
            identifier=unlimited_identifier,
            backend=inmemory_backend,
            registry=ThrottleRegistry(),
        )
        app = FastAPI()

        @app.get("/", dependencies=[Depends(throttle)], status_code=200)
        async def ping_endpoint() -> typing.Dict[str, str]:
            return {"message": "PONG"}

        async with make_client(app, base_url="http://0.0.0.0") as client:
            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 200

            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers.get("Retry-After", None) is None

    async def test_throttle_dependency_not_in_openapi_schema(self, app: FastAPI) -> None:
        """
        Regression test: Using a throttle as a dependency (via Depends) should not
        leak its internal parameters (cost, context, etc.) into the OpenAPI schema.
        """
        throttle = HTTPThrottle(
            "test-openapi-schema-fa",
            rate="10/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.post("/items", dependencies=[Depends(throttle)], status_code=201)
        async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
            return {"name": item.name, "price": item.price}

        schema = app.openapi()
        post_op = schema["paths"]["/items"]["post"]

        # The request body schema should only reference ItemModel, not any
        # throttle-internal parameters.
        request_body = post_op.get("requestBody", {})
        body_schema = (
            request_body.get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        assert "cost" not in body_schema.get("properties", {}), (
            "Throttle 'cost' param leaked into OpenAPI schema"
        )
        assert "context" not in body_schema.get("properties", {}), (
            "Throttle 'context' param leaked into OpenAPI schema"
        )

        params = post_op.get("parameters", [])
        param_names = {p["name"] for p in params}
        assert "cost" not in param_names, (
            "Throttle 'cost' appeared as a query/path parameter"
        )
        assert "context" not in param_names, (
            "Throttle 'context' appeared as a query/path parameter"
        )

    async def test_throttle_dependency_does_not_force_body_embed(
        self, app: FastAPI
    ) -> None:
        """
        Regression test: A throttle used as a dependency should not force
        Body(embed=True) on a Pydantic model parameter.

        Previously, when the throttle's __call__ had explicit `cost` and `context`
        kwargs, FastAPI would interpret them as additional body fields, forcing the
        single Pydantic body param to be nested under its name key (embed behavior).

        With the fix, sending `{"name": "Widget", "price": 9.99}` directly as the
        request body should work -- no need to wrap it as `{"item": {...}}`.
        """
        throttle = HTTPThrottle(
            "test-body-embed-fa",
            rate="100/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.post("/create", dependencies=[Depends(throttle)], status_code=201)
        async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
            return {"name": item.name, "price": item.price}

        async with make_client(app, base_url="http://test") as client:
            response = await client.post(
                "/create", json={"name": "Widget", "price": 9.99}
            )
            assert response.status_code == 201, (
                f"Expected 201, got {response.status_code}. "
                f"Body parsing may have been affected by throttle dependency. "
                f"Response: {response.json()}"
            )
            assert response.json() == {"name": "Widget", "price": 9.99}

    async def test_throttle_decorator_does_not_force_body_embed(
        self, app: FastAPI
    ) -> None:
        """Same as above but using the @throttled() FastAPI DI decorator."""
        throttle = HTTPThrottle(
            "test-decorator-body-embed-fa",
            rate="100/s",
            identifier=default_client_identifier,
            registry=ThrottleRegistry(),
        )

        @app.post("/create-decorated", status_code=201)
        @throttled(throttle)
        async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
            return {"name": item.name, "price": item.price}

        async with make_client(app, base_url="http://test") as client:
            response = await client.post(
                "/create-decorated", json={"name": "Gadget", "price": 19.99}
            )
            assert response.status_code == 201, (
                f"Expected 201, got {response.status_code}. "
                f"Body parsing may have been affected by @throttled decorator. "
                f"Response: {response.json()}"
            )
            assert response.json() == {"name": "Gadget", "price": 19.99}

            schema = app.openapi()
            post_op = schema["paths"]["/create-decorated"]["post"]
            body_schema = (
                post_op.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            assert "cost" not in body_schema.get("properties", {}), (
                "Throttle 'cost' param leaked into OpenAPI schema via @throttled"
            )
            assert "context" not in body_schema.get("properties", {}), (
                "Throttle 'context' param leaked into OpenAPI schema via @throttled"
            )
