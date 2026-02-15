# Direct Usage

Sometimes you need the throttle to behave differently based on runtime information, such as the size of an uploaded file, the number of records in a query, or whether a previous step succeeded. Direct usage lets you call the throttle methods yourself from inside a handler, giving you full programmatic control.

---

## When to use direct usage

- **Dynamic cost** — the cost of a request depends on something you compute at runtime (e.g., payload size, number of items).
- **Multi-step workflows** — you want to consume quota only after a successful operation, or conditionally skip throttling based on an intermediate result.
- **Conditional throttling** — different branches of your handler have different rate limit semantics.
- **Per-message WebSocket throttling** — you want to throttle each message inside an established WebSocket connection, not just the connection attempt.

For simpler cases, [dependency injection](dependencies.md) or [decorators](decorators.md) are usually less code.

---

## `await throttle.hit(connection)`: check and apply

`hit()` is the core method. It records a hit, checks the quota, and rejects the request if the limit is exceeded. Returns the connection object.

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:data", rate="100/min")

@app.get("/data")
async def get_data(request: Request):
    await throttle.hit(request)
    return {"data": []}
```

If the client is throttled, `hit()` raises an exception by default (handled by Traffik's default throttled handler, which returns a `429` response), or uses your custom throttled handler if provided.

---

## `await throttle.hit(connection, cost=N)`: custom cost per call

Pass a `cost` argument to override the throttle's default cost for this specific call. Useful when different invocations of the same endpoint should consume different amounts of quota.

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

# Rate limit: 1000 "units" per minute
upload_throttle = HTTPThrottle(uid="uploads", rate="1000/min")

@app.post("/upload")
async def upload_file(request: Request):
    body = await request.body()
    # Charge 1 unit per KB, minimum 1
    cost = max(1, len(body) // 1024)

    await upload_throttle.hit(request, cost=cost)

    # Process upload...
    return {"bytes_received": len(body)}
```

!!! tip
    A cost of `0` is a no-op. Traffik short-circuits immediately without recording anything or checking the backend. You can use this to conditionally skip throttling without an `if` branch:

    ```python
    cost = 0 if request.headers.get("X-Internal") == "true" else 1
    await throttle.hit(request, cost=cost)
    ```

---

## `await throttle(connection, context={...})`: `__call__` alias

Calling the throttle instance directly is an alias for `hit()`. The `context` keyword argument lets you pass extra information that throttle strategies, identifiers, or rules can read.

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:scoped", rate="200/min")

@app.get("/resource/{tenant_id}")
async def get_resource(tenant_id: str, request: Request):
    # Pass tenant context so different tenants have separate quota buckets
    await throttle(request, context={"scope": tenant_id})
    return {"tenant": tenant_id}
```

The `context` dict is merged with the throttle's default context (set at construction time). Call-time values take precedence over defaults in case of key conflicts.

---

## `await throttle.check(connection, cost=N)`: non-consuming pre-check

`check()` inspects the current quota state without consuming any quota. It returns `True` if there is sufficient quota available for the given cost, `False` otherwise.

!!! warning "Best-effort only"
    `check()` is inherently subject to race conditions (Time-of-Check to Time-of-Use). Between the check and the eventual `hit()`, another request may consume the remaining quota. Use `check()` only for fast pre-screening, always follow up with `hit()` to actually consume quota.

```python
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

export_throttle = HTTPThrottle(uid="exports", rate="1000/min")

@app.post("/export")
async def export_data(request: Request):
    body = await request.body()
    cost = max(1, len(body) // 512)

    # Pre-check before running the expensive export
    if not await export_throttle.check(request, cost=cost):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit would be exceeded. This request costs {cost} units.",
        )

    # Quota looks sufficient — proceed with the expensive operation
    result = await run_export(body)

    # Consume quota now that the operation completed
    await export_throttle.hit(request, cost=cost)

    return result
```

When the throttle strategy doesn't support stats (e.g., a custom strategy without a `get_stat` method), `check()` returns `True` optimistically rather than blocking the request.

---

## `await throttle.stat(connection)`: read state without consuming

`stat()` returns a `StrategyStat` object with the current throttle state for the connection. No quota is consumed. Returns `None` if the strategy doesn't support stats.

The `StrategyStat` object contains:

| Field | Type | Description |
|---|---|---|
| `hits_remaining` | `int` or `float` | Quota remaining in the current window |
| `wait_ms` | `float` | Milliseconds until quota resets (0 if quota is available) |
| `rate` | `Rate` | The rate this stat is for |
| `key` | `str` | The namespaced throttle key used for this connection |

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:metered", rate="100/min")

@app.get("/status")
async def quota_status(request: Request):
    stat = await throttle.stat(request)

    if stat is None:
        return {"quota": "unknown"}

    return {
        "hits_remaining": stat.hits_remaining,
        "retry_after_ms": stat.wait_ms,
    }
```

A common pattern is to read the stat *before* consuming quota to include rate limit information in the response:

```python
@app.get("/data")
async def get_data(request: Request):
    stat_before = await throttle.stat(request)
    await throttle.hit(request)

    return {
        "data": [],
        "quota_used": 1,
        "quota_remaining": (stat_before.hits_remaining - 1) if stat_before else None,
    }
```

---

## WebSocket: `is_throttled(websocket)` — check state after hit

For WebSocket connections, `hit()` doesn't raise an exception on throttle by default. Instead it sends a `rate_limit` JSON message to the client and marks the connection state. Use `is_throttled()` to check whether the last hit was throttled.

```python
from fastapi import FastAPI, WebSocket
from traffik.throttles import WebSocketThrottle, is_throttled
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

ws_throttle = WebSocketThrottle(uid="ws:messages", rate="60/min")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    while True:
        message = await websocket.receive_text()

        # Record a hit for each message received
        await ws_throttle.hit(websocket)

        if is_throttled(websocket):
            # Client was notified by the throttle handler.
            # Optionally close the connection after repeated violations.
            await websocket.close(code=1008, reason="Rate limit exceeded")
            return

        # Process the message normally
        await websocket.send_text(f"Echo: {message}")
```

!!! note
    `is_throttled()` reads a flag that `hit()` sets on `websocket.state`. It reflects the result of the **most recent** `hit()` call only. If you call `hit()` again, the flag is updated.

---

## Putting it together: dynamic cost with conditional throttling

This example shows a multi-step pattern: compute cost from the request body, pre-check availability, then consume quota only on success.

```python
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

# Each request costs based on number of items processed
# Total budget: 10,000 items per minute
batch_throttle = HTTPThrottle(uid="batch:items", rate="10000/min")

@app.post("/batch")
async def process_batch(request: Request):
    data = await request.json()
    items = data.get("items", [])

    if not items:
        return {"processed": 0}

    cost = len(items)

    # Non-consuming check first
    if not await batch_throttle.check(request, cost=cost):
        stat = await batch_throttle.stat(request)
        retry_after = int((stat.wait_ms / 1000) + 1) if stat else 60
        raise HTTPException(
            status_code=429,
            detail=f"Batch of {cost} items would exceed quota.",
            headers={"Retry-After": str(retry_after)},
        )

    # Run the actual processing
    results = await process_items(items)

    # Consume quota — actual cost may differ if some items failed
    actual_cost = len(results)
    await batch_throttle.hit(request, cost=actual_cost)

    return {"processed": actual_cost, "results": results}
```

--8<-- "includes/abbreviations.md"
