# Decorators

The `@throttled` decorator applies throttling directly to a route function. It keeps the rate limit configuration visible right above the handler, which some developers find easier to scan than a `dependencies=[...]` list on the route decorator.

Traffik ships **two versions** of `@throttled` — one for Starlette and one for FastAPI. They behave differently in how the throttle receives the connection object.

---

## Two decorator variants

| | `traffik.throttles.throttled` | `traffik.decorators.throttled` |
|---|---|---|
| **Works with** | Starlette and FastAPI | FastAPI only |
| **Requires `Request` param** | Yes — the route function must declare a `Request` or `WebSocket` parameter | No — FastAPI's DI injects the connection automatically |
| **Mechanism** | Inspects function args for an `HTTPConnection` at call time | Wraps the route with a hidden `Depends(throttle)` |
| **OpenAPI impact** | None | None |
| **Recommended for** | Starlette apps; Starlette-compatible code | FastAPI apps |

---

## `traffik.decorators.throttled` — FastAPI version

Import from `traffik.decorators`. Your route function does **not** need to declare a `Request` parameter — FastAPI resolves the connection behind the scenes.

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled  # FastAPI-specific

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="items:read", rate="100/min")

@app.get("/items")
@throttled(throttle)
async def list_items():
    return {"items": []}
```

The decorator order matters: `@app.get(...)` must be the outermost decorator, `@throttled(...)` the next one in.

!!! tip
    Because `traffik.decorators.throttled` relies on FastAPI's dependency injection, the throttle check happens before any other dependencies in the function signature are resolved. This means a rejected request never triggers database queries or other expensive dependencies.

---

## `traffik.throttles.throttled` — Starlette version

Import from `traffik.throttles` (or directly from `traffik`). The route function **must** declare a `Request` or `WebSocket` parameter — the decorator inspects the function arguments at call time to find the connection object.

```python
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import throttled  # Starlette version

backend = InMemoryBackend()

throttle = HTTPThrottle(uid="items:read", rate="100/min")

@throttled(throttle)
async def list_items(request: Request) -> JSONResponse:
    return JSONResponse({"items": []})

app = Starlette(
    routes=[Route("/items", list_items, methods=["GET"])],
    lifespan=backend.lifespan,
)
```

!!! warning "The Starlette decorator requires an `HTTPConnection` parameter"
    If the decorated function has no `Request` or `WebSocket` parameter (positional or keyword), Traffik raises a `ValueError` at call time. For FastAPI routes without a `Request` parameter, use `traffik.decorators.throttled` instead.

The Starlette version also works in FastAPI if you prefer it — just make sure your handler declares the `Request`.

```python
from fastapi import FastAPI
from starlette.requests import Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import throttled  # Starlette version, used in FastAPI

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="items:read-sl", rate="100/min")

@app.get("/items")
@throttled(throttle)
async def list_items(request: Request):  # <-- required for starlette.throttles version
    return {"items": []}
```

---

## Multiple throttles with `@throttled`

Pass multiple throttles to `@throttled`. They are checked **sequentially** — the first throttle is checked first, and if it rejects the request, the remaining throttles are never consulted.

=== "FastAPI decorator"

    ```python
    from fastapi import FastAPI
    from traffik import HTTPThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.decorators import throttled

    backend = InMemoryBackend()
    app = FastAPI(lifespan=backend.lifespan)

    burst_throttle = HTTPThrottle(uid="search:burst", rate="10/s")
    sustained_throttle = HTTPThrottle(uid="search:sustained", rate="200/min")

    @app.get("/search")
    @throttled(burst_throttle, sustained_throttle)
    async def search(q: str):
        return {"query": q, "results": []}
    ```

=== "Starlette decorator"

    ```python
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from traffik import HTTPThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.throttles import throttled

    backend = InMemoryBackend()

    burst_throttle = HTTPThrottle(uid="search:burst", rate="10/s")
    sustained_throttle = HTTPThrottle(uid="search:sustained", rate="200/min")

    @throttled(burst_throttle, sustained_throttle)
    async def search(request: Request) -> JSONResponse:
        q = request.query_params.get("q", "")
        return JSONResponse({"query": q, "results": []})

    app = Starlette(
        routes=[Route("/search", search, methods=["GET"])],
        lifespan=backend.lifespan,
    )
    ```

!!! note "Sequential checking: first failure stops the rest"
    With `@throttled(first, second, third)`, if `first` rejects the request, `second` and `third` are never evaluated. This is efficient — no unnecessary quota checks — but it also means the order of throttles matters. Put the most restrictive (or cheapest to evaluate) throttle first.

---

## WebSocket routes

`@throttled` works with WebSocket routes too. Use `WebSocketThrottle` and apply the decorator the same way. This throttles at the **connection** level — one hit is recorded when the WebSocket handshake occurs.

=== "FastAPI decorator"

    ```python
    from fastapi import FastAPI, WebSocket
    from traffik.throttles import WebSocketThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.decorators import throttled

    backend = InMemoryBackend()
    app = FastAPI(lifespan=backend.lifespan)

    ws_throttle = WebSocketThrottle(uid="ws:connect", rate="30/min")

    @app.websocket("/ws")
    @throttled(ws_throttle)
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"status": "connected"})
        await websocket.close()
    ```

=== "Starlette decorator"

    ```python
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocket
    from traffik.throttles import WebSocketThrottle, throttled
    from traffik.backends.inmemory import InMemoryBackend

    backend = InMemoryBackend()

    ws_throttle = WebSocketThrottle(uid="ws:connect", rate="30/min")

    @throttled(ws_throttle)
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"status": "connected"})
        await websocket.close()

    app = Starlette(
        routes=[WebSocketRoute("/ws", websocket_endpoint)],
        lifespan=backend.lifespan,
    )
    ```

For per-message throttling inside an established WebSocket connection, see [Direct Usage](direct-usage.md).

---

## Decorators vs. dependencies — when to prefer each

Both approaches produce the same runtime behavior. The choice is largely a matter of style:

- **Decorators** keep the throttle configuration adjacent to the handler. If you scan a file top-to-bottom, the rate limit is immediately visible before you read the function body.
- **Dependencies** keep route configuration in the route decorator. Useful when you want all route metadata — status codes, response models, dependencies — in one place.

You can mix them freely. A common pattern is to use a router-level dependency for a broad limit and a decorator for a tight per-handler limit:

```python
from fastapi import FastAPI, APIRouter, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

global_throttle = HTTPThrottle(uid="api:global", rate="1000/min")
export_throttle = HTTPThrottle(uid="api:export", rate="5/min")

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(global_throttle)],
)

@router.get("/data")
async def get_data():
    return {"data": []}

@router.get("/export")
@throttled(export_throttle)
async def export_data():
    # hits global_throttle (via router dep) + export_throttle (via decorator)
    return {"export": "..."}

app.include_router(router)
```
