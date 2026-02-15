# Context-Aware Backends (Multi-Tenant)

By default, a throttle picks a backend once — at startup — and uses it for every
request it ever sees. That's the right call for most apps. But in a multi-tenant
SaaS, "one backend for everyone" creates a problem: tenant A's traffic eats into
tenant B's quota counters, or worse, their limits bleed into each other.

`dynamic_backend=True` solves this. Instead of caching a backend at startup, the
throttle asks "which backend should I use right now?" on every request, reading the
answer from the current async context. Your middleware sets the right backend before
the request reaches the throttle; the throttle picks it up automatically.

---

## When Do You Need This?

| Scenario | Use `dynamic_backend`? |
|---|---|
| Single shared backend for all users | No — pass `backend=...` directly |
| Different Redis instances per customer tier | **Yes** |
| A/B testing different backends per route | **Yes** |
| Swapping backends in tests (nested context managers) | **Yes** |
| One Redis cluster, namespaced by tenant | No — use a custom `identifier` instead |

!!! warning "Don't reach for this by default"
    `dynamic_backend=True` adds 1–20 ms of overhead per request (one context variable
    lookup + one `initialize()` call if the backend hasn't started yet). For simple
    shared backends, the `backend=...` parameter is always faster and simpler.

---

## How It Works

Traffik uses a Python `ContextVar` to track the "active backend" in the current
async task. When `dynamic_backend=True`, the throttle reads from this contextvar on
every `hit(...)` call instead of using a cached reference.

Your middleware is responsible for pushing the right backend into context *before*
the route handler runs. The key tool is the backend's async context manager:

```python
async with my_backend(app, close_on_exit=False, persistent=True):
    # The ContextVar now points to `my_backend` for the duration of this block.
    # Any dynamic-backend throttle called here will use it.
    response = await call_next(request)
```

The `close_on_exit=False` flag is important in middleware: you don't want to close
and reset the backend connection after every single request. `persistent=True` means
the backend's stored data survives the context exit (the counters aren't wiped).

---

## Setting Up a Dynamic Throttle

Creating a dynamic throttle is the same as a normal throttle, except you omit
`backend` and set `dynamic_backend=True`. Traffik will raise a `ValueError` if you
provide both.

```python
from traffik import HTTPThrottle

api_throttle = HTTPThrottle(
    uid="api:quota",
    rate="1000/hour",
    dynamic_backend=True,    # Resolve backend from context at request time
    identifier=get_user_id,  # Still identify clients the usual way
)

# api_throttle.backend is None — that's expected and correct.
```

---

## Full Multi-Tenant Example

This example models a SaaS with three tiers:

- **Enterprise**: dedicated Redis instance, isolated completely.
- **Premium**: shared Redis pool, separate from the free tier.
- **Free**: in-memory backend, sufficient for lower traffic.

The middleware reads the tenant's tier from the `Authorization` header and activates
the corresponding backend for the duration of that request.

```python
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.redis import RedisBackend

# One throttle, three different backends depending on tenant tier
api_throttle = HTTPThrottle(
    uid="api:quota",
    rate="1000/hour",
    dynamic_backend=True,
)

# Backend instances — created once at module level, NOT per-request
enterprise_backend = RedisBackend(
    "redis://enterprise-redis:6379",
    namespace="enterprise",
    persistent=True,
)
premium_backend = RedisBackend(
    "redis://shared-redis:6379",
    namespace="premium",
    persistent=True,
)
free_backend = InMemoryBackend(persistent=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start all backends at application startup
    async with (
        enterprise_backend(app, close_on_exit=False, persistent=True),
        premium_backend(app, close_on_exit=False, persistent=True),
        free_backend(app, close_on_exit=False, persistent=True),
    ):
        yield
    # All backends close cleanly when the app shuts down


class TenantRoutingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        tier = decode_tenant_tier(auth)  # Your JWT/token logic here

        if tier == "enterprise":
            async with enterprise_backend(close_on_exit=False, persistent=True):
                return await call_next(request)

        elif tier == "premium":
            async with premium_backend(close_on_exit=False, persistent=True):
                return await call_next(request)

        else:
            # Free tier: in-memory (or a shared Redis with tight limits)
            async with free_backend(close_on_exit=False, persistent=True):
                return await call_next(request)


app = FastAPI(lifespan=lifespan)
app.add_middleware(TenantRoutingMiddleware)  # type: ignore[arg-type]


@app.get("/api/data", dependencies=[Depends(api_throttle)])
async def get_data():
    return {"data": "..."}
```

When an enterprise request arrives, the middleware wraps `call_next` in
`enterprise_backend(...)`. The throttle calls `get_backend(connection)`, finds
`enterprise_backend` in the ContextVar, and uses it — all without any modification
to the route handler.

---

## The `backend(app, close_on_exit=False, persistent=True)` Pattern

You'll see this pattern everywhere in dynamic backend code. Here's what each
parameter does:

| Parameter | Value | Meaning |
|---|---|---|
| `app` | Your FastAPI/Starlette app | Registers the backend on `app.state` so it's also available to non-async code |
| `close_on_exit` | `False` | Don't close the Redis connection after each request — that would be catastrophic |
| `persistent` | `True` | Don't wipe the rate limit counters on context exit — they need to persist between requests |

!!! warning "Never use `persistent=False` with middleware"
    Without `persistent=True`, every request would clear the rate limit counters
    for that backend on exit. Your throttle would effectively be disabled — every
    client would start fresh on every request.

---

## Testing with Nested Contexts

`dynamic_backend=True` also makes testing much cleaner. You can nest context
managers to switch backends mid-test without creating multiple throttle instances.

```python
import pytest
from starlette.requests import Request
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

@pytest.mark.asyncio
async def test_different_backends_isolated():
    throttle = HTTPThrottle(
        uid="test:dynamic",
        rate="2/min",
        dynamic_backend=True,
    )

    backend_a = InMemoryBackend(persistent=True)
    backend_b = InMemoryBackend(persistent=True)

    request = make_dummy_request()  # Helper that builds a Request object

    async with backend_a():
        await throttle(request)  # Uses backend_a — hit 1
        await throttle(request)  # Uses backend_a — hit 2

        async with backend_b():
            # Inner context switches to backend_b — fresh counter
            await throttle(request)  # Uses backend_b — hit 1 (not throttled!)
            await throttle(request)  # Uses backend_b — hit 2

        # Back to backend_a — counter is still at 2
        # Third hit in backend_a will be throttled
```

This is the main reason `dynamic_backend` was designed — it makes integration tests
with per-test isolation trivial.

---

## Performance Impact

| Operation | Overhead |
|---|---|
| Static backend (normal `backend=...`) | ~0 ms — cached attribute lookup |
| Dynamic backend (ContextVar read) | 1–5 ms typical, up to 20 ms under contention |

The overhead comes from reading the `ContextVar` and potentially calling
`initialize()` if the backend hasn't been set up yet for that context. This is
negligible for most APIs, but worth knowing if you're optimizing a hot path with
sub-10ms latency requirements.

!!! tip "If you only have one backend, don't use `dynamic_backend`"
    The overhead of dynamic backend resolution is only justified when you genuinely
    need different backends for different requests. If you're running a single
    Redis cluster and just want to namespace keys per tenant, a custom `identifier`
    function is the right tool:

    ```python
    async def tenant_identifier(request: Request):
        tenant_id = extract_tenant_id(request)
        client_ip = request.client.host
        return f"{tenant_id}:{client_ip}"

    # One backend, but counters are automatically isolated per tenant
    throttle = HTTPThrottle(
        uid="api:quota",
        rate="1000/hour",
        backend=shared_redis_backend,
        identifier=tenant_identifier,
    )
    ```

---

## Anti-Patterns

!!! warning "Don't create backends inside the middleware `dispatch` method"
    Creating a new backend object on every request is expensive and defeats connection
    pooling. Create backend instances once at module level (or in lifespan), then
    activate them in middleware via their context manager.

    ```python
    # Bad: new backend object every request
    class BadMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            backend = RedisBackend("redis://...", namespace="app")  # Don't do this
            async with backend():
                return await call_next(request)

    # Good: backend created once, reused across requests
    _backend = RedisBackend("redis://...", namespace="app", persistent=True)

    class GoodMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            async with _backend(close_on_exit=False, persistent=True):
                return await call_next(request)
    ```

!!! warning "Don't mix `dynamic_backend=True` with an explicit `backend=...`"
    Traffik raises a `ValueError` at construction time if you try:

    ```python
    # This raises ValueError
    throttle = HTTPThrottle(
        uid="bad",
        rate="100/min",
        dynamic_backend=True,
        backend=my_backend,   # Can't combine these
    )
    ```
