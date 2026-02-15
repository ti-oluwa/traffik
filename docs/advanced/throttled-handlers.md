# Custom Throttled Handlers

So a client hits the limit. What happens next?

By default, Traffik handles it for you: HTTP throttles raise a `429 Too Many Requests` exception, and WebSocket throttles send a JSON message. For most apps that's perfectly fine.

But "most apps" isn't "every app". Maybe you want to include a `Retry-After` header with an exact timestamp. Maybe your WebSocket protocol has a specific message format for rate limit events. Maybe your API team has strong opinions about error response shapes.

That's what custom throttled handlers are for.

---

## Default Behavior

Before you customize, it helps to know what you're replacing:

- **HTTPThrottle**: Raises `ConnectionThrottled` which becomes an HTTP `429` response with a plain text body.
- **WebSocketThrottle**: If the connection is open, sends a JSON message and keeps the connection alive. If the connection isn't open yet, it raises `ConnectionThrottled`.

The WebSocket default message looks like this:

```json
{
    "type": "rate_limit",
    "error": "Too many messages",
    "retry_after": 5
}
```

---

## Handler Signature

All throttled handlers — HTTP and WebSocket — share the same signature:

```python
async def handler(
    connection,   # Request or WebSocket
    wait_ms,      # float: milliseconds until client can retry
    throttle,     # the Throttle instance that triggered this
    context,      # dict: throttle context
) -> Any:
    ...
```

The `wait_ms` value is already in milliseconds. Convert to seconds with `math.ceil(wait_ms / 1000)` for human-friendly messages.

---

## Custom HTTP Handler

### Custom Headers and Status Code

```python
import math
from fastapi import FastAPI, Request, Depends
from starlette.responses import JSONResponse
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)


async def my_http_handler(request, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)
    raise Exception  # We raise, not return, from HTTP handlers

# Actually: HTTP handlers raise an HTTP response, not return one.
# The cleanest approach is to raise a Starlette HTTPException with custom headers:

from starlette.exceptions import HTTPException as StarletteHTTPException

async def rate_limit_handler(request, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)
    raise StarletteHTTPException(
        status_code=429,
        detail={
            "error": "rate_limit_exceeded",
            "message": f"You've hit the {throttle.uid!r} limit. Slow down!",
            "retry_after_seconds": retry_after,
            "limit": throttle.rate.limit if not callable(throttle.rate) else "dynamic",
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Reset": str(retry_after),
        },
    )


throttle = HTTPThrottle(
    "api:read",
    rate="100/min",
    backend=backend,
    handle_throttled=rate_limit_handler,
)
```

### Include Stats in the Response

If you want to include `X-RateLimit-Remaining` in the error response, call `throttle.stat()` inside the handler:

```python
import math
from starlette.exceptions import HTTPException as StarletteHTTPException

async def rich_rate_limit_handler(request, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)

    # Read stats without consuming quota
    stat = await throttle.stat(request, context)
    headers = {"Retry-After": str(retry_after)}
    if stat:
        headers["X-RateLimit-Limit"] = str(int(stat.rate.limit))
        headers["X-RateLimit-Remaining"] = "0"

    raise StarletteHTTPException(
        status_code=429,
        detail={"error": "too_many_requests", "retry_after": retry_after},
        headers=headers,
    )
```

---

## Custom WebSocket Handler

WebSocket handlers have two styles: send a message (recommended) or raise an exception.

### Option 1: Send a Message (Recommended)

The connection stays open. The client gets notified and can back off gracefully:

```python
import math
from starlette.websockets import WebSocketState

async def ws_rate_limit_handler(websocket, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)

    if websocket.application_state == WebSocketState.CONNECTED:
        await websocket.send_json({
            "type": "rate_limit",
            "error": "message_rate_exceeded",
            "retry_after": retry_after,
            "throttle": throttle.uid,
        })
    # Don't raise — just return. The connection stays alive.
```

!!! tip "Performance note"
    Sending a message is significantly faster than raising an exception in WebSocket handlers. Exception propagation has overhead. For high-frequency message throttling (think chat apps or telemetry streams), the send-and-return pattern is measurably better.

### Option 2: Raise an Exception (Not Recommended)

This closes the connection from the server side. Only use it when you genuinely want to disconnect the client:

```python
from traffik.exceptions import ConnectionThrottled

async def ws_strict_handler(websocket, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)
    # This raises ConnectionThrottled which terminates the WebSocket
    raise ConnectionThrottled(
        wait_period=retry_after,
        detail="Too many messages. Reconnect after the wait period.",
    )
```

!!! warning "Raising in WebSocket handlers"
    Raising inside a WebSocket throttled handler closes the connection. That's usually not what you want for message-level throttling. Use it only for connection-level throttling (e.g., max connections per IP at connect time).

### Close the Connection on Throttle (Code 1008)

If you want to explicitly close the WebSocket with a policy violation close code:

```python
from starlette.websockets import WebSocketState, WebSocketDisconnect
import math

async def ws_close_handler(websocket, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)
    if websocket.application_state == WebSocketState.CONNECTED:
        await websocket.close(code=1008, reason=f"Rate limit exceeded. Retry after {retry_after}s.")
```

WebSocket close code `1008` is the standard "Policy Violation" code — the right semantic choice for rate limiting.

---

## Backend-Level Default Handler

Instead of setting a handler on each throttle, you can set a default at the backend level. All throttles that use this backend will inherit it (unless they override it with their own handler):

```python
from traffik.backends.redis import RedisBackend
import math
from starlette.exceptions import HTTPException as StarletteHTTPException

async def default_handler(connection, wait_ms, throttle, context):
    retry_after = math.ceil(wait_ms / 1000)
    raise StarletteHTTPException(
        status_code=429,
        detail=f"Rate limit exceeded. Retry in {retry_after}s.",
        headers={"Retry-After": str(retry_after)},
    )

backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    handle_throttled=default_handler,   # Applied to all throttles on this backend
)
```

Throttle-level handlers take precedence over backend-level handlers. If a throttle defines its own `handle_throttled`, the backend default is ignored for that throttle.

---

## Quick Reference

| Scenario | Recommendation |
|---|---|
| Custom HTTP error shape | Raise `HTTPException` with custom `detail` and `headers` |
| WebSocket: notify but keep alive | Send JSON message, then return |
| WebSocket: disconnect client | `await websocket.close(code=1008)` |
| Apply to all throttles on a backend | Set `handle_throttled` on the backend |
| Per-throttle override | Pass `handle_throttled` to `HTTPThrottle(...)` |
