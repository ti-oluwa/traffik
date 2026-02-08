import asyncio
import typing
from itertools import repeat

import anyio
import pytest
from fastapi import Depends, FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from tests.asynctestclient import AsyncTestClient
from tests.conftest import BackendGen
from tests.utils import default_client_identifier, unlimited_identifier
from traffik import strategies
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.rates import Rate
from traffik.throttles import HTTPThrottle, Throttle, WebSocketThrottle


@pytest.fixture(scope="function")
def lifespan_app(inmemory_backend: InMemoryBackend) -> FastAPI:
    """
    Lifespan fixture for FastAPI app to ensure proper startup and shutdown.
    """
    app = FastAPI(lifespan=inmemory_backend.lifespan)
    return app


@pytest.mark.asyncio
@pytest.mark.throttle
@pytest.mark.fastapi
async def test_throttle_initialization(inmemory_backend: InMemoryBackend) -> None:
    with pytest.raises(ValueError):
        Throttle("test-init-1", rate="-1/s")

    async def _throttle_handler(connection: HTTPConnection, *args, **kwargs) -> None:
        # do nothing, just a placeholder for testing
        return

    # Test initialization behaviour
    async with inmemory_backend(close_on_exit=True):
        throttle = Throttle(
            "test-init-2",
            rate=Rate(limit=2, milliseconds=10, seconds=50, minutes=2, hours=1),
            handle_throttled=_throttle_handler,
        )
        time_in_ms = 10 + (50 * 1000) + (2 * 60 * 1000) + (1 * 3600 * 1000)
        assert throttle.rate.expire == time_in_ms  # type: ignore[union-attr]
        assert throttle.backend is inmemory_backend
        assert throttle.identifier is inmemory_backend.identifier
        # Test that provided throttle handler is used
        assert throttle.handle_throttled is not inmemory_backend.handle_throttled


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_with_app_lifespan(lifespan_app: FastAPI) -> None:
    throttle = HTTPThrottle(
        "test-throttle-app-lifespan",
        rate="2/s",
        identifier=default_client_identifier,
    )

    @lifespan_app.get(
        "/",
        dependencies=[Depends(throttle)],
        status_code=200,
    )
    async def ping_endpoint() -> typing.Dict[str, str]:
        return {"message": "PONG"}

    base_url = "http://0.0.0.0"
    with TestClient(lifespan_app, base_url=base_url) as client:
        # First request should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Second request should also succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Third request should be throttled
        response = client.get("/")
        assert response.status_code == 429
        assert response.headers["Retry-After"] is not None


def test_throttle_exemption_with_unlimited_identifier(
    inmemory_backend: InMemoryBackend,
) -> None:
    throttle = HTTPThrottle(
        "test-throttle-exemption",
        rate="2/s",
        identifier=unlimited_identifier,
        backend=inmemory_backend,
    )
    app = FastAPI()

    @app.get(
        "/",
        dependencies=[Depends(throttle)],
        status_code=200,
    )
    async def ping_endpoint() -> typing.Dict[str, str]:
        return {"message": "PONG"}

    base_url = "http://0.0.0.0"
    with TestClient(app, base_url=base_url) as client:
        # First request should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Second request should also succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "PONG"}

        # Third request should be throttled but since the identifier is EXEMPTED,
        # it should not be throttled and should succeed
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers.get("Retry-After", None) is None


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.fastapi
async def test_http_throttle(backends: BackendGen) -> None:
    for backend in backends(persistent=False, namespace="http_throttle_test"):
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(
                "test-http-throttle",
                rate="3/3005ms",
            )
            sleep_time = 4 + (5 / 1000)

            app = FastAPI()

            @app.get(
                "/{name}",
                dependencies=[Depends(throttle)],
                status_code=200,
            )
            async def ping_endpoint(name: str) -> typing.Dict[str, str]:
                return {"message": f"PONG: {name}"}

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url=base_url,
            ) as client:
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    if count == 4:
                        await anyio.sleep(sleep_time)
                    response = await client.get(f"{base_url}/{name}")
                    assert response.status_code == 200
                    assert response.json() == {"message": f"PONG: {name}"}

                await backend.reset()
                await backend.initialize()
                for count, name in enumerate(repeat("test-client", 5), start=1):
                    response = await client.get(f"/{name}")
                    if count > 3:
                        assert response.status_code == 429
                        assert response.headers["Retry-After"] is not None
                    else:
                        assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.throttle
@pytest.mark.concurrent
@pytest.mark.fastapi
async def test_http_throttle_concurrent(backends: BackendGen) -> None:
    for backend in backends(persistent=False, namespace="http_throttle_concurrent"):
        async with backend(close_on_exit=True):
            throttle = HTTPThrottle(
                "http-throttle-concurrent",
                rate="3/s",
                strategy=strategies.TokenBucketStrategy(),
            )
            app = FastAPI()

            @app.get(
                "/{name}",
                dependencies=[Depends(throttle)],
                status_code=200,
            )
            async def ping_endpoint(name: str) -> typing.Dict[str, str]:
                return {"message": f"PONG: {name}"}

            base_url = "http://0.0.0.0"
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url=base_url,
            ) as client:

                async def make_request(name: str) -> Response:
                    return await client.get(f"{base_url}/{name}")

                responses = await asyncio.gather(
                    *(make_request(name) for name in repeat("test-client", 5))
                )
                status_codes = [r.status_code for r in responses]
                assert status_codes.count(200) == 3
                assert status_codes.count(429) == 2


@pytest.mark.asyncio
@pytest.mark.throttle
@pytest.mark.websocket
@pytest.mark.fastapi
async def test_websocket_throttle(backends: BackendGen) -> None:
    for backend in backends(persistent=False, namespace="ws_throttle_test"):
        async with backend(close_on_exit=True):
            throttle = WebSocketThrottle(
                "test-websocket-throttle-inmemory",
                rate="3/5005ms",
                identifier=default_client_identifier,
            )

            app = FastAPI()

            @app.websocket("/ws/")
            async def ws_endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                print("ACCEPTED WEBSOCKET CONNECTION")
                close_code = 1000  # Normal closure
                close_reason = "Normal closure"
                while True:
                    try:
                        data = await websocket.receive_json()
                        await throttle(websocket)
                        await websocket.send_json(
                            {
                                "status": "success",
                                "status_code": 200,
                                "headers": {},
                                "detail": "Request successful",
                                "data": data,
                            }
                        )
                    except HTTPException as exc:
                        print("HTTP EXCEPTION:", exc)
                        await websocket.send_json(
                            {
                                "status": "error",
                                "status_code": exc.status_code,
                                "detail": exc.detail,
                                "headers": exc.headers,
                                "data": None,
                            }
                        )
                        close_reason = exc.detail
                        break
                    except Exception as exc:
                        print("WEBSOCKET ERROR:", exc)
                        await websocket.send_json(
                            {
                                "status": "error",
                                "status_code": 500,
                                "detail": "Operation failed",
                                "headers": {},
                                "data": None,
                            }
                        )
                        close_code = 1011  # Internal error
                        close_reason = "Internal error"
                        break

                # Allow time for the message put in the queue to be processed
                # and received by the client before closing the websocket
                await asyncio.sleep(1)
                await websocket.close(code=close_code, reason=close_reason)

            base_url = "http://0.0.0.0"
            running_loop = asyncio.get_running_loop()
            async with (
                AsyncTestClient(
                    app=app,
                    base_url=base_url,
                    event_loop=running_loop,
                ) as client,
                client.websocket_connect(url="/ws/") as ws,
            ):
                # Reset the backend before starting the test
                # as connecting to the websocket already counts as a request
                # and we want to start fresh.
                await backend.reset()
                await backend.initialize()

                async def make_ws_request() -> typing.Tuple[str, int]:
                    try:
                        await ws.send_json({"message": "ping"})
                        response = await ws.receive_json()
                        return response["status"], response["status_code"]
                    except WebSocketDisconnect as exc:
                        print("WEBSOCKET DISCONNECT:", exc)
                        return "disconnected", 1000

                for count in range(1, 6):
                    result = await make_ws_request()
                    assert result[0] == "success"
                    assert result[1] == 200
                    if count == 3:
                        sleep_time = 5.5  # For the last request, we wait a bit longer
                        await asyncio.sleep(sleep_time)

                # Clear backend to ensure the throttle is cleared
                await backend.reset()
                await backend.initialize()

                await asyncio.sleep(0.01)
                for count in range(1, 4):
                    result = await make_ws_request()
                    if count > 3:
                        # After the third request, the throttle should kick in
                        # and subsequent requests should fail
                        assert result[0] == "error"
                        assert result[1] == 429
                    else:
                        assert result[0] == "success"
                        assert result[1] == 200


class ItemModel(BaseModel):
    name: str
    price: float


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dependency_not_in_openapi_schema(
    lifespan_app: FastAPI,
) -> None:
    """
    Regression test: Using a throttle as a dependency (via Depends) should not
    leak its internal parameters (cost, context, etc.) into the OpenAPI schema.
    """
    throttle = HTTPThrottle(
        "test-openapi-schema",
        rate="10/s",
        identifier=default_client_identifier,
    )

    @lifespan_app.post(
        "/items",
        dependencies=[Depends(throttle)],
        status_code=201,
    )
    async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
        return {"name": item.name, "price": item.price}

    schema = lifespan_app.openapi()
    post_op = schema["paths"]["/items"]["post"]

    # The request body schema should only reference ItemModel,
    # not any throttle-internal parameters
    request_body = post_op.get("requestBody", {})
    body_schema = (
        request_body.get("content", {}).get("application/json", {}).get("schema", {})
    )

    # If throttle params leaked, the body schema would have "properties"
    # with "cost", "context", etc., or the ItemModel would be nested under
    # an embed key. The schema should reference ItemModel directly.
    assert "cost" not in body_schema.get("properties", {}), (
        "Throttle 'cost' param leaked into OpenAPI schema"
    )
    assert "context" not in body_schema.get("properties", {}), (
        "Throttle 'context' param leaked into OpenAPI schema"
    )

    # The operation's parameters list should not contain throttle params either
    params = post_op.get("parameters", [])
    param_names = {p["name"] for p in params}
    assert "cost" not in param_names, (
        "Throttle 'cost' appeared as a query/path parameter"
    )
    assert "context" not in param_names, (
        "Throttle 'context' appeared as a query/path parameter"
    )


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_dependency_does_not_force_body_embed(
    lifespan_app: FastAPI,
) -> None:
    """
    Regression test: A throttle used as a dependency should not force
    Body(embed=True) on a Pydantic model parameter.

    Previously, when the throttle's __call__ had explicit `cost` and `context`
    kwargs, FastAPI would interpret them as additional body fields, forcing the
    single Pydantic body param to be nested under its name key (embed behavior).

    With the fix, sending `{"name": "Widget", "price": 9.99}` directly as the
    request body should work — no need to wrap it as `{"item": {...}}`.
    """
    throttle = HTTPThrottle(
        "test-body-embed",
        rate="100/s",
        identifier=default_client_identifier,
    )

    @lifespan_app.post(
        "/create",
        dependencies=[Depends(throttle)],
        status_code=201,
    )
    async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
        return {"name": item.name, "price": item.price}

    base_url = "http://test"
    with TestClient(lifespan_app, base_url=base_url) as client:
        # Send the body directly as the model — NOT embedded under "item" key
        response = client.post("/create", json={"name": "Widget", "price": 9.99})
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}. "
            f"Body parsing may have been affected by throttle dependency. "
            f"Response: {response.json()}"
        )
        assert response.json() == {"name": "Widget", "price": 9.99}


@pytest.mark.throttle
@pytest.mark.fastapi
def test_throttle_decorator_does_not_force_body_embed(
    lifespan_app: FastAPI,
) -> None:
    """
    Regression test: Same as above but using the @throttled() decorator
    instead of Depends(throttle).
    """
    throttle = HTTPThrottle(
        "test-decorator-body-embed",
        rate="100/s",
        identifier=default_client_identifier,
    )

    @lifespan_app.post("/create-decorated", status_code=201)
    @throttled(throttle)
    async def create_item(item: ItemModel) -> typing.Dict[str, typing.Any]:
        return {"name": item.name, "price": item.price}

    base_url = "http://test"
    with TestClient(lifespan_app, base_url=base_url) as client:
        response = client.post(
            "/create-decorated", json={"name": "Gadget", "price": 19.99}
        )
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}. "
            f"Body parsing may have been affected by @throttled decorator. "
            f"Response: {response.json()}"
        )
        assert response.json() == {"name": "Gadget", "price": 19.99}

        # Also verify the schema is clean
        schema = lifespan_app.openapi()
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
