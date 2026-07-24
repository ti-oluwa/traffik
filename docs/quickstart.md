# Quick Start

Let's go from zero to a rate-limited API. Each step builds on the last, so by the end you'll have a solid mental model of how Traffik works and a production-ready example to copy from.

---

## Step 1: The Simplest Possible Setup

One backend, one throttle, one endpoint.

```python
# main.py
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

# 1. Create a backend. InMemory needs no configuration.
backend = InMemoryBackend(namespace="myapp")

# 2. Give FastAPI the backend's lifespan (initializes on startup, cleans up on shutdown).
app = FastAPI(lifespan=backend.lifespan)

# 3. Create a throttle with a unique ID and a rate.
throttle = HTTPThrottle("api:items", rate="100/min")

# 4. Attach it to your route as a FastAPI dependency.
@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    return {"items": ["widget", "gizmo", "thingamajig"]}
```

Run it:

```bash
uvicorn main:app --reload
```

Send more than 100 requests in a minute and you'll get:

```text
HTTP 429 Too Many Requests
Retry-After: 42
```

!!! tip "The `uid` must be unique"
    Every throttle needs a unique string ID (`uid`). It is what Traffik uses to namespace state for that throttle in the backend. A good convention is `"feature:purpose"`, e.g, `"auth:login"`, `"api:v2:search"`. If you reuse a `uid` across two different throttle instances, you'll get a `ConfigurationError` at startup.

---

## Step 2: The Backend + Lifespan Pattern

Backends hold open connections (Redis, Memcached) and run background cleanup tasks. `lifespan` sets those up before your first request and tears them down cleanly on shutdown.

`backend.lifespan` is just a normal Starlette/FastAPI lifespan context manager, so if you already have one, compose them:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Your own startup logic here (database connections, etc.)
    async with backend(app):  # Initializes the backend
        yield
    # Your own shutdown logic runs after `yield`

app = FastAPI(lifespan=lifespan)
```

!!! info "What `backend(app)` does"
    `async with backend(app)` registers the backend as the *default* for that application. Any `HTTPThrottle` that doesn't specify an explicit `backend=` discovers and uses it automatically. This is the common case, and it's what `backend.lifespan` does for you under the hood.

---

## Step 3: Using It as a Dependency

By now you've atleast seen the `dependencies=[Depends(throttle)]` usage. There's a second style: inject the throttle as a parameter and get the request (or whatever `starlette.request.HTTPConnection` type) object back from it directly:

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:items", rate="100/min")

@app.get("/items")
async def list_items(request: Request = Depends(throttle)):
    # `request` is the real FastAPI Request object. Traffik returns it
    # once the rate limit check passes.
    return {"items": [], "client_ip": request.client.host}
```

Both styles behave identically. The first is cleaner when you don't need the request in the handler; the second saves you a separate `Request` parameter.

### Stacking multiple throttles

You can enforce several limits at once on a single endpoint. Say a burst limit and a sustained limit:

```python
burst_throttle = HTTPThrottle("uploads:burst", rate="10/min")
sustained_throttle = HTTPThrottle("uploads:sustained", rate="100/hour")

@app.post("/upload", dependencies=[Depends(burst_throttle), Depends(sustained_throttle)])
async def upload_file():
    # Both limits must pass. Either one being exceeded returns 429.
    return {"status": "uploaded"}
```

!!! tip "Router-level throttling"
    Apply a throttle to a whole router so every endpoint under it shares the limit:

    ```python
    from fastapi import APIRouter, Depends
    from traffik import HTTPThrottle

    router_throttle = HTTPThrottle("api:v1:global", rate="1000/min")
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(router_throttle)])

    @router.get("/users")
    async def list_users():
        return {"users": []}

    @router.get("/products")
    async def list_products():
        return {"products": []}

    # /api/v1/users and /api/v1/products now share one 1000/min budget.
    ```

---

## Step 4: The Decorator Style

Prefer decorators? For FastAPI routes, `traffik.decorators.throttled` skips the extra `Request` parameter entirely:

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled  # FastAPI-specific import

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

burst = HTTPThrottle("search:burst", rate="5/min")
sustained = HTTPThrottle("search:sustained", rate="50/hour")

@app.get("/search")
@throttled(burst, sustained)  # Both throttles apply
async def search():
    return {"results": []}
```

!!! warning "There are two `throttled` imports and they're not the same thing"
    | Import | Framework | Route needs `Request`? |
    |---|---|---|
    | `from traffik.decorators import throttled` | FastAPI only - uses dependency injection under the hood | No |
    | `from traffik.throttles import throttled` | Starlette + FastAPI - a plain wrapper decorator | Yes |

    For Starlette routes, use `traffik.throttles.throttled` and give your route a `Request` parameter. For FastAPI, prefer `traffik.decorators.throttled` for the cleaner signature, but the plain one works there too if you'd rather have one code path for both frameworks.

---

## Step 5: A Production-Ready Setup

In production you'll typically want Redis for shared state, a circuit breaker so backend hiccups don't cascade into your whole API  endpoint failing, and a fallback backend as a safety net. Here's a complete setup:

```python
# main.py - production configuration
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.redis.aioredis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.strategies import SlidingWindowCounter
from traffik.error_handlers import failover, CircuitBreaker

# --- Backends ---

# Primary: Redis (distributed, persistent)
redis_backend = RedisBackend(
    "redis://localhost:6379/0",
    namespace="myapp:prod",
    persistent=True,           # State survives a Redis restart
    lock_type="redis",         # Single-node lock (lower latency than redlock)
    lock_ttl=5.0,               # Auto-release a held lock after 5 seconds
    lock_blocking_timeout=2.0,  # Give up waiting on a lock after 2 seconds
)

# Fallback: InMemory (used only when Redis is unavailable)
fallback_backend = InMemoryBackend(namespace="myapp:fallback")

# --- Circuit breaker ---
# Opens after 10 consecutive failures, tries recovery after 60 seconds
breaker = CircuitBreaker(
    failure_threshold=10,
    recovery_timeout=60.0,
    success_threshold=3,  # Needs 3 successes before it fully closes again
)

# --- Application ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with redis_backend(app, persistent=True):
        await fallback_backend.initialize()
        yield
        await fallback_backend.close()

app = FastAPI(lifespan=lifespan)

# --- Throttles ---

api_throttle = HTTPThrottle(
    "api:v1",
    rate="1000/hour",
    strategy=SlidingWindowCounter(),  # More accurate than FixedWindow at the boundary
    backend=redis_backend,
    on_error=failover(                # On a Redis error, try the fallback backend
        backend=fallback_backend,
        breaker=breaker,
        max_retries=2,
    ),
)

@app.get("/api/data")
async def get_data(request: Request = Depends(api_throttle)):
    return {"data": "value", "client": request.client.host}
```

!!! tip "`SlidingWindowCounter` vs `FixedWindow`"
    `FixedWindow` is the default stratgy used by throttles. It's fast, memory-light, and use one atomic increment per hit. Its tradeoff is boundary bursts - 100 requests at 11:59:59 plus another 100 at 12:00:00 is 200 requests in two seconds, both technically within their own window. `SlidingWindowCounter` smooths that out for not much extra cost. Use it when burst behavior at window edges actually matters for your API.

!!! warning "Lock types matter at scale"
    `lock_type="redis"` is a single-node lock (~0.4ms overhead in our benchmarks). Across multiple independent Redis clusters, use `lock_type="redlock"` (the Redlock algorithm, ~5ms overhead) instead. Pick based on your actual topology.Redlock on a single node just costs you latency for nothing.

### Rate string formats

Traffik parses a fairly wide range of natural rate strings:

```python
HTTPThrottle("api:a", rate="100/minute")    # 100 per minute
HTTPThrottle("api:b", rate="5/10seconds")   # 5 per 10 seconds
HTTPThrottle("api:c", rate="1000/500ms")    # 1000 per 500 milliseconds
HTTPThrottle("api:d", rate="20 per 2 mins") # 20 per 2 minutes
HTTPThrottle("api:e", rate="2 per second")  # 2 per second
```

Or build a `Rate` directly for periods that don't fit a single unit cleanly:

```python
from traffik.rates import Rate

# 100 requests per 5 minutes and 30 seconds
rate = Rate(limit=100, minutes=5, seconds=30)
HTTPThrottle("api:complex", rate=rate)
```

---

## Step 6: WebSocket Rate Limiting

Traffik throttles WebSockets at two levels:

1. **Connection level** - gate the handshake itself (reject new connections once the limit is hit).
2. **Message level** - allow the connection, then rate-limit individual messages within it.

You can use one throttle for both or use separate ones if the connection are rated differently.

A handler doing both:

```python
from fastapi import FastAPI, WebSocket, Depends
from traffik import WebSocketThrottle, is_throttled
from traffik.backends.redis.aioredis import RedisBackend

backend = RedisBackend("redis://localhost:6379/0", namespace="ws")
app = FastAPI(lifespan=backend.lifespan)

# Max 10 simultaneous connections from the same IP
connection_throttle = WebSocketThrottle("ws:connect", rate="10/min")

# Max 60 messages per minute within a connection
message_throttle = WebSocketThrottle("ws:messages", rate="60/min")

@app.websocket("/ws/chat")
async def chat_endpoint(
    websocket: WebSocket = Depends(connection_throttle),  # (1)!
):
    await websocket.accept()

    close_code = 1000
    reason = "Normal closure"

    try:
        while True:
            data = await websocket.receive_json()

            await message_throttle(websocket, context={"scope": "chat"})  # (2)!
            if is_throttled(websocket):  # (3)!
                continue

            response = {"echo": data, "type": "message"}
            await websocket.send_json(response)

    except Exception:
        close_code = 1011
        reason = "Internal error"

    await websocket.close(code=close_code, reason=reason)
```

1. Gates the connection itself. Exceed the connection limit before the handshake completes and the client gets `HTTP/1.1 429 Too Many Requests` - the WebSocket never opens.
2. Called on every incoming frame (with `scope=chat` which reflects in the request identifier).
3. `True` if the last call throttled the message. The client already has a `rate_limit` JSON frame by this point - skip processing but keep the connection open.

!!! info "What the client sees when throttled"
    A throttled WebSocket message doesn't get silently dropped or the connection closed, instead the client gets a JSON frame:

    ```json
    {
      "type": "rate_limit",
      "error": "Too many messages",
      "retry_after": 1
    }
    ```

    `retry_after` is in whole seconds. Want different behavior, like closing the connection instead? Provide your own `handle_throttled` callable on the throttle.

---

## Custom Identifiers

By default Traffik identifies clients by IP. For authenticated APIs you almost always want user or API key instead:

```python
from starlette.requests import Request
from traffik import HTTPThrottle, EXEMPTED

# Identify by user ID
async def user_identifier(request: Request) -> str:
    user_id = request.state.user_id  # set by your auth middleware
    if user_id is None:
        return request.client.host  # anonymous users fall back to IP
    return f"user:{user_id}"

# Identify by API key, exempting internal services
async def api_key_identifier(request: Request) -> str:
    api_key = request.headers.get("x-api-key", "")
    if await is_internal(api_key):
        return EXEMPTED  # bypasses throttling entirely for this connection
    return f"key:{api_key}"

throttle = HTTPThrottle(
    "api:user-scoped",
    rate="500/hour",
    identifier=user_identifier,
)
```

!!! tip "The `EXEMPTED` sentinel"
    Returning `EXEMPTED` from an identifier function bypasses throttling completely for that connection. You don't need a separate whitelist logic anywhere else in your throttle config.

---

## What's Next?

You know the essential patterns. From here:

- **[Core Concepts](core-concepts/index.md)** - rates, backends, strategies, and identifiers, in depth.
- **[Integration Guide](integration/index.md)** - dependencies, decorators, middleware, direct calls.
- **[Advanced Features](advanced/index.md)** - rules, request costs, exemptions, quota contexts, and more.
- **[Error Handling](error-handling.md)** - circuit breakers, failover, custom error handlers.
- **[Testing](testing.md)** - testing throttled endpoints without fighting your own rate limiter.
