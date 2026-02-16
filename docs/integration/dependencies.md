# Dependency Injection

FastAPI's `Depends` mechanism is the most idiomatic way to attach throttles to routes. Traffik throttle instances are callable and expose a clean `__signature__` that FastAPI understands natively, slotting in exactly like any other dependency.

!!! tip "Recommended approach for FastAPI"
    Dependency injection is the standard FastAPI pattern. It composes cleanly with other dependencies, works at the route and router level, and doesn't require you to change your handler signature.

!!! note "Works with plain Starlette too"
    `Depends(throttle)` is a FastAPI convenience, but the underlying throttle works with **any Starlette `Request`**. If you are using plain Starlette (without FastAPI), you can call the throttle directly in your route handler:

    ```python
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from traffik import HTTPThrottle

    throttle = HTTPThrottle(uid="api:items", rate="100/min")

    async def list_items(request: Request):
        await throttle.hit(request)  # raises ConnectionThrottled if over limit
        return JSONResponse({"items": []})
    ```

    FastAPI's `Depends` is simply a wrapper around this same call, resolving the `Request` and calling the throttle for you. In plain Starlette, you manage the call yourself.

---

## Basic usage — no Request access needed

The simplest setup: pass the throttle as a list dependency on the route decorator. Your handler function doesn't need to declare any extra parameter.

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:items", rate="100/min")

@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": []}
```

FastAPI resolves the throttle before executing the handler. If the client exceeds the rate limit, the request is rejected with a `429 Too Many Requests` response before your handler is ever called.

---

## With `Request` access

If your handler also needs the request object, for example to read headers or query parameters, declare the throttle as a named parameter. FastAPI injects both the `Request` and the throttle result.

```python
from fastapi import FastAPI, Depends, Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:search", rate="30/min")

@app.get("/search")
async def search(
    q: str,
    request: Request = Depends(throttle),
):
    client_ip = request.client.host
    return {"query": q, "from": client_ip}
```

!!! note
    `Depends(throttle)` returns the `Request` (or `WebSocket`) object after it has been checked. Naming the parameter `request` and annotating it with `Request = Depends(throttle)` gives you the familiar request object without adding any extra function calls.

---

## Multiple throttles on one route

Apply several throttles to the same route by adding them to the `dependencies` list. Each throttle is checked in the order it appears. The first one to reject the request stops processing.

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

burst_throttle = HTTPThrottle(uid="api:burst", rate="20/s")
sustained_throttle = HTTPThrottle(uid="api:sustained", rate="500/min")

@app.post(
    "/ingest",
    dependencies=[
        Depends(burst_throttle),
        Depends(sustained_throttle),
    ],
)
async def ingest_data(payload: dict):
    return {"accepted": True}
```

A common pattern is to combine a tight **burst** throttle with a looser **sustained** throttle. The burst throttle prevents sudden spikes; the sustained throttle caps total volume over a longer window.

---

## Router-level dependencies

Add a throttle to an `APIRouter` and it applies automatically to every route registered on that router. You don't need to repeat `Depends(throttle)` on each individual route.

```python
from fastapi import FastAPI, APIRouter, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

api_throttle = HTTPThrottle(uid="api:global", rate="1000/min")

api_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(api_throttle)],
)

@api_router.get("/users")
async def list_users():
    return {"users": []}

@api_router.get("/products")
async def list_products():
    return {"products": []}

app.include_router(api_router)
```

### Layering router and route throttles

Router-level and route-level dependencies stack. A request to `/api/v1/admin/report` in the example below hits both `api_throttle` (from the router) and `report_throttle` (from the route).

```python
from fastapi import FastAPI, APIRouter, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

api_throttle = HTTPThrottle(uid="api:v1", rate="1000/min")
report_throttle = HTTPThrottle(uid="api:reports", rate="10/min")

api_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(api_throttle)],
)
admin_router = APIRouter(prefix="/admin")

@admin_router.get(
    "/report",
    dependencies=[Depends(report_throttle)],
)
async def generate_report():
    return {"status": "generating"}

api_router.include_router(admin_router)
app.include_router(api_router)
```

!!! tip "Router-level throttles are the cleanest way to enforce limits on entire API sections"
    Define one throttle per router boundary (e.g., public vs. authenticated vs. admin) rather than repeating dependencies on dozens of individual routes.

---

## WebSocket routes

The same pattern works for WebSocket routes. Use `WebSocketThrottle` and declare the dependency the same way.

```python
from fastapi import FastAPI, Depends, WebSocket
from traffik.throttles import WebSocketThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

ws_throttle = WebSocketThrottle(uid="ws:chat", rate="60/min")

@app.websocket("/ws/chat")
async def chat(websocket: WebSocket = Depends(ws_throttle)):
    await websocket.accept()
    while True:
        data = await websocket.receive_text()
        await websocket.send_text(f"Echo: {data}")
```

!!! note
    For WebSocket throttles that apply per-message (rather than per-connection), use [direct usage](direct-usage.md) inside the message loop instead.


