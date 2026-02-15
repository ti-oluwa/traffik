# Middleware

`ThrottleMiddleware` sits in the ASGI stack and intercepts every request before it reaches any route handler. Use it when you want to enforce rate limits globally or based on path/method patterns, without touching individual route definitions.

!!! note "Works with any Starlette application"
    `ThrottleMiddleware` is a standard Starlette middleware and works with **any Starlette-based application** — not just FastAPI. If you're using plain Starlette or any other ASGI framework built on Starlette, you can add `ThrottleMiddleware` the same way: `app.add_middleware(ThrottleMiddleware, middleware_throttles=[...])`.

!!! tip "Use middleware for cross-cutting concerns"
    Middleware is the right tool when the throttle logic is independent of which specific route is hit — for example, a global IP-based limit, or a limit on all `/api/` traffic regardless of endpoint. For per-route or per-handler limits, prefer [dependency injection](dependencies.md).

---

## Basic setup

Wrap your underlying throttle in `MiddlewareThrottle` and pass it to `ThrottleMiddleware`. The backend is typically shared via the `lifespan` context.

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="global", rate="1000/min")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(throttle),
    ],
)
```

This applies the throttle to every HTTP request. `MiddlewareThrottle` without any `path`, `methods`, or `predicate` arguments matches all connections.

---

## HTTP and WebSocket throttles in middleware

`ThrottleMiddleware` handles both HTTP and WebSocket connections. Pass throttles for each connection type in the same `middleware_throttles` list — Traffik routes them automatically based on the connection's ASGI scope type.

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.throttles import WebSocketThrottle

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

http_throttle = HTTPThrottle(uid="http:global", rate="500/min")
ws_throttle = WebSocketThrottle(uid="ws:global", rate="60/min")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(http_throttle),
        MiddlewareThrottle(ws_throttle, path="/ws/"),
    ],
)
```

HTTP throttles are never applied to WebSocket connections, and vice versa. Traffik categorizes throttles by connection type at startup, so there is no per-request branching overhead.

---

## Path filtering

The `path` argument on `MiddlewareThrottle` limits the throttle to requests whose URL path matches. Strings are interpreted as **prefix matches** (compiled to a regex internally). You can also pass a pre-compiled `re.Pattern`.

=== "String prefix"

    A plain string matches any path that starts with that prefix.

    ```python
    from traffik import HTTPThrottle
    from traffik.middleware import MiddlewareThrottle

    throttle = HTTPThrottle(uid="api", rate="300/min")

    # Matches /api/, /api/users, /api/v2/products, etc.
    api_middleware_throttle = MiddlewareThrottle(throttle, path="/api/")
    ```

=== "Compiled regex"

    Pass a `re.Pattern` for full regex control.

    ```python
    import re
    from traffik import HTTPThrottle
    from traffik.middleware import MiddlewareThrottle

    throttle = HTTPThrottle(uid="api:versioned", rate="300/min")

    # Matches /api/v1/..., /api/v2/..., but not /api/legacy/...
    pattern = re.compile(r"^/api/v\d+/")
    versioned_middleware_throttle = MiddlewareThrottle(throttle, path=pattern)
    ```

!!! note
    When `path` is a string, it is compiled as a regex pattern. If you use a bare string like `"/api/"`, it matches any path that contains `/api/` at that position (anchored from the start). Use `re.compile(...)` explicitly when you need full regex semantics such as anchors or alternation.

!!! note "Wildcard patterns are supported"
    Because `MiddlewareThrottle`'s `path` parameter uses `ThrottleRule` under the hood, it supports the same wildcard syntax: `*` matches a single path segment (no `/`), and `**` matches any number of segments including `/`. For example, `path="/api/*/users"` matches `/api/v1/users` and `/api/v2/users` but not `/api/v1/admin/users`. See [Throttle Rules & Wildcards](../advanced/rules.md) for the full pattern reference.

---

## Method filtering

The `methods` argument restricts the throttle to specific HTTP verbs. WebSocket connections ignore method filtering entirely (they have no HTTP method after the handshake).

```python
from traffik import HTTPThrottle
from traffik.middleware import MiddlewareThrottle

write_throttle = HTTPThrottle(uid="writes", rate="100/min")

# Only applies to POST, PUT, PATCH, and DELETE requests
write_middleware_throttle = MiddlewareThrottle(
    write_throttle,
    methods={"POST", "PUT", "PATCH", "DELETE"},
)
```

GET requests to any path pass through this throttle without consuming quota.

---

## Predicate filtering

For conditions that can't be expressed with path or method alone, provide an async `predicate` callable. The throttle only applies when the predicate returns `True`.

```python
import typing
from starlette.requests import HTTPConnection
from traffik import HTTPThrottle
from traffik.middleware import MiddlewareThrottle

throttle = HTTPThrottle(uid="premium:api", rate="2000/min")

async def is_premium_user(
    connection: HTTPConnection,
    context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
) -> bool:
    return connection.headers.get("X-User-Tier") == "premium"

premium_middleware_throttle = MiddlewareThrottle(
    throttle,
    path="/api/",
    predicate=is_premium_user,
)
```

!!! warning "Keep predicates fast"
    The predicate runs on every matching request before the throttle strategy executes. Avoid blocking I/O or expensive computation. If you need database lookups, consider caching the result in `request.state` from an earlier middleware.

---

## Combined: path + methods + predicate

All three filters are evaluated conjunctively — the throttle only fires when **all** conditions are met.

```python
import typing
from starlette.requests import HTTPConnection
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from fastapi import FastAPI

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

upload_throttle = HTTPThrottle(uid="uploads:authenticated", rate="50/hour")

async def is_authenticated(
    connection: HTTPConnection,
    context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
) -> bool:
    return "Authorization" in connection.headers

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(
            upload_throttle,
            path="/api/uploads/",
            methods={"POST"},
            predicate=is_authenticated,
        )
    ],
)
```

---

## Execution order within a throttle

For each `MiddlewareThrottle`, filters are evaluated from **cheapest to most expensive**:

1. **Methods** — a set membership check, essentially free.
2. **Path** — a compiled regex match against the URL string.
3. **Predicate** — your async callable, potentially involving I/O.

If any check fails, the remaining checks are skipped and the throttle is bypassed for that request. The throttle strategy (quota consumption) only runs after all filters pass.

---

## Throttle sorting

When `ThrottleMiddleware` receives a list of `MiddlewareThrottle` instances, it sorts them before processing. Sorting controls which throttles run first, which is most useful when you want cheap (low-cost) throttles to reject requests before expensive ones do unnecessary work.

The `sort` parameter accepts:

| Value | Behavior |
|---|---|
| `"cheap_first"` (default) | Throttles with lower `cost` run first |
| `"cheap_last"` | Throttles with higher `cost` run first |
| `False` or `None` | No sorting; use the order you provided |
| A callable | Sorted by your custom key function |

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

cheap_throttle = HTTPThrottle(uid="cheap", rate="1000/min")
expensive_throttle = HTTPThrottle(uid="expensive", rate="100/min")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(expensive_throttle, cost=10),
        MiddlewareThrottle(cheap_throttle, cost=1),
    ],
    sort="cheap_first",  # cheap_throttle runs first (default behavior)
)
```

!!! note "Indeteminable thottle cost is treated as infinite"
    A `MiddlewareThrottle` with no explicit `cost` (i.e., `cost=None`), and the wrapped `Throttle` uses a dynamic cost function, is treated as having infinite cost and sorted **last** under `"cheap_first"`. This ensures unconstrained throttles don't block cheap ones from short-circuiting early.

### Custom sort key

Provide a callable that takes a `MiddlewareThrottle` (or `Throttle`) and returns any sortable value:

```python
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[...],
    sort=lambda t: t.throttle.uid,  # sort alphabetically by throttle UID
)
```

---

## Using `MiddlewareThrottle` as a FastAPI dependency

`MiddlewareThrottle` implements `__call__` and exposes a `__signature__` compatible with FastAPI's dependency injection. This is a niche use case — for example, when you have a `MiddlewareThrottle` instance configured with path and method filters, and you also want to use the same filtered throttle as a route-level dependency.

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="api:write", rate="50/min")
write_middleware_throttle = MiddlewareThrottle(
    throttle,
    methods={"POST", "PUT", "DELETE"},
)

# Used as a dependency — the path/method filters still apply
@app.post("/resource", dependencies=[Depends(write_middleware_throttle)])
async def create_resource(payload: dict):
    return {"created": True}
```

!!! note
    When used as a dependency, `MiddlewareThrottle` applies its path and method filters using the actual request. This means you get the same filtered behavior as in middleware, but scoped to a specific route. This is rarely needed — prefer plain `Depends(throttle)` for route-level throttling.

---

## Complete middleware example

```python
import re
import typing
from fastapi import FastAPI
from starlette.requests import HTTPConnection
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.throttles import WebSocketThrottle

backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)

# Global IP-based limit for all traffic
global_throttle = HTTPThrottle(uid="global", rate="2000/min")

# Stricter limit for write operations on the API
write_throttle = HTTPThrottle(uid="api:writes", rate="200/min")

# Per-connection limit for WebSocket
ws_throttle = WebSocketThrottle(uid="ws:connections", rate="30/min")

async def is_write_method(
    connection: HTTPConnection,
    context: typing.Optional[typing.Mapping[str, typing.Any]] = None,
) -> bool:
    return connection.scope.get("method", "") in {"POST", "PUT", "PATCH", "DELETE"}

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(global_throttle, cost=1),
        MiddlewareThrottle(
            write_throttle,
            path=re.compile(r"^/api/"),
            predicate=is_write_method,
            cost=5,
        ),
        MiddlewareThrottle(ws_throttle, path="/ws/"),
    ],
    sort="cheap_first",
)

@app.get("/api/data")
async def get_data():
    return {"data": []}

@app.post("/api/data")
async def create_data(payload: dict):
    return {"created": True}
```
