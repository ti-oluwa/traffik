# Quick Start

Let's go from zero to a rate-limited API in a few minutes. Each step builds on the last, so by the end you'll have a solid mental model of how Traffik works - and a production-ready example to draw from.

---

## Step 1: The Simplest Possible Setup

Let's start with the absolute minimum: one backend, one throttle, one endpoint.

```python
# main.py
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

# 1. Create a backend. InMemory needs no configuration.
backend = InMemoryBackend(namespace="myapp")

# 2. Tell FastAPI to use the backend's lifespan (initialize on startup, clean up on shutdown).
app = FastAPI(lifespan=backend.lifespan)

# 3. Create a throttle with a unique ID and a rate limit.
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

Now send more than 100 requests in a minute and you'll get:

```
HTTP 429 Too Many Requests
Retry-After: 42
```

!!! tip "The `uid` must be unique"
    Every throttle needs a unique string ID (`uid`). This is what Traffik uses to store and look up state in the backend. A good convention is `"feature:purpose"`, like `"auth:login"` or `"api:v2:search"`. Don't reuse UIDs across different throttle instances or you'll get a `ConfigurationError` at startup.

---

## Step 2: Understanding the Backend + Lifespan Pattern

Why does the backend need a lifespan? Because backends hold open connections (to Redis, Memcached, etc.) and run background cleanup tasks. The lifespan ensures those are set up before your first request and torn down cleanly when you shut down.

The `backend.lifespan` is just a standard Starlette/FastAPI lifespan context manager. If you already have a lifespan, compose them:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Your startup logic here (e.g., database connections)
    async with backend(app):  # Initialize the backend
        yield
    # Your shutdown logic runs after `yield`

app = FastAPI(lifespan=lifespan)
```

!!! info "What `backend(app)` does"
    Calling the backend as a context manager (`async with backend(app)`) registers it as the *default* backend for that application. Any `HTTPThrottle` that doesn't specify an explicit backend will automatically discover and use it. This is the most common pattern.

---

## Step 3: Using It as a Dependency

You've already seen the `dependencies=[Depends(throttle)]` pattern. There's a second way: inject the throttle as a parameter to get access to the request object in the same dependency call:

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:items", rate="100/min")

@app.get("/items")
async def list_items(request: Request = Depends(throttle)):
    # `request` is the actual FastAPI Request object.
    # Traffik returns it after checking the rate limit.
    client_ip = request.client.host
    return {"items": [], "client_ip": client_ip}
```

Both styles work identically. The first (`dependencies=[...]`) is cleaner when you don't need the request. The second gives you the request object inside the handler.

### Applying multiple throttles

Stack throttles to enforce multiple limits simultaneously - for example, a per-minute burst limit and a per-hour sustained limit:

```python
burst_throttle    = HTTPThrottle("uploads:burst",     rate="10/min")
sustained_throttle = HTTPThrottle("uploads:sustained", rate="100/hour")

@app.post("/upload", dependencies=[Depends(burst_throttle), Depends(sustained_throttle)])
async def upload_file():
    # Both limits must pass. If either is exceeded, 429 is returned.
    return {"status": "uploaded"}
```

!!! tip "Router-level throttling"
    Apply a throttle to an entire router so all endpoints under it share the same limit:

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

    # Both /api/v1/users and /api/v1/products now share the 1000/min limit.
    ```

---

## Step 4: The Decorator Style

Prefer decorators? Traffik has you covered. For FastAPI routes, use `traffik.decorators.throttled` - your route doesn't need an explicit `Request` parameter:

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled  # FastAPI-specific import

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

burst     = HTTPThrottle("search:burst",     rate="5/min")
sustained = HTTPThrottle("search:sustained", rate="50/hour")

@app.get("/search")
@throttled(burst, sustained)  # Apply both throttles
async def search():
    return {"results": []}
```

!!! warning "Two `throttled` imports - pick the right one"
    There are two versions of `throttled`:

    | Import | Framework | Route needs `Request`? |
    |---|---|---|
    | `from traffik.decorators import throttled` | FastAPI only | No |
    | `from traffik.throttles import throttled` | Starlette + FastAPI | Yes |

    For Starlette routes, use `traffik.throttles.throttled` and make sure your route function has a `Request` parameter. For FastAPI, prefer `traffik.decorators.throttled` - cleaner signatures, no extra parameters.

---

## Step 5: A Production-Ready Setup

In production, you'll want Redis for distributed state, a circuit breaker so backend failures don't cascade, and a fallback backend as a safety net. Here's a complete setup:

```python
# main.py - Production configuration
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.strategies import SlidingWindowCounter
from traffik.error_handlers import failover, CircuitBreaker

# --- Backends ---

# Primary: Redis (distributed, persistent)
redis_backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="myapp:prod",
    persistent=True,           # State survives Redis restarts
    lock_type="redis",         # Single-instance lock (lower latency than redlock)
    lock_ttl=5.0,              # Auto-release locks after 5 seconds
    lock_blocking_timeout=2.0, # Give up waiting for a lock after 2 seconds
)

# Fallback: InMemory (used when Redis is unavailable)
fallback_backend = InMemoryBackend(namespace="myapp:fallback")

# --- Circuit Breaker ---
# Opens after 10 failures, attempts recovery after 60 seconds
breaker = CircuitBreaker(
    failure_threshold=10,
    recovery_timeout=60.0,
    success_threshold=3,  # Requires 3 successes before fully reopening
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
    strategy=SlidingWindowCounter(),  # More accurate than Fixed Window
    backend=redis_backend,
    on_error=failover(                 # On Redis error: try the fallback backend
        backend=fallback_backend,
        breaker=breaker,
        max_retries=2,
    ),
)

@app.get("/api/data")
async def get_data(request: Request = Depends(api_throttle)):
    return {"data": "value", "client": request.client.host}
```

!!! tip "Sliding Window Counter vs Fixed Window"
    The default strategy is `FixedWindow`, which is fast and memory-efficient. But it allows a burst of 2x your limit at window boundaries (100 requests at 11:59:59 + 100 at 12:00:00 = 200 in two seconds). `SlidingWindowCounter` smooths this out with minimal overhead. Use it for APIs where burst behavior matters.

!!! warning "Lock types matter at scale"
    `lock_type="redis"` uses a single-node Redis lock (~0.4ms overhead). For deployments across multiple Redis clusters, use `lock_type="redlock"` (Redlock algorithm, ~5ms overhead). Pick based on your topology, not habit.

### Rate string formats

Traffik understands many natural rate string formats, so you can write what feels readable:

```python
HTTPThrottle("api:a", rate="100/minute")    # 100 per minute
HTTPThrottle("api:b", rate="5/10seconds")   # 5 per 10 seconds
HTTPThrottle("api:c", rate="1000/500ms")    # 1000 per 500 milliseconds
HTTPThrottle("api:d", rate="20 per 2 mins") # 20 per 2 minutes
HTTPThrottle("api:e", rate="2 per second")  # 2 per second
```

Or use the `Rate` object directly for complex periods:

```python
from traffik import Rate

# 100 requests per 5 minutes and 30 seconds
rate = Rate(limit=100, minutes=5, seconds=30)
HTTPThrottle("api:complex", rate=rate)

# Unlimited (no throttling)
HTTPThrottle("api:open", rate="0/0")
```

---

## Step 6: WebSocket Rate Limiting

Traffik treats WebSocket connections as first-class citizens. You can throttle at two levels:

1. **Connection level** - Gate the WebSocket handshake (reject new connections when over the limit).
2. **Message level** - Allow the connection but rate-limit individual messages within it.

Here's a complete WebSocket handler that does both:

```python
from fastapi import FastAPI, WebSocket, Depends
from traffik import WebSocketThrottle, is_throttled
from traffik.backends.redis import RedisBackend

backend = RedisBackend(connection="redis://localhost:6379/0", namespace="ws")
app = FastAPI(lifespan=backend.lifespan)

# Throttle WebSocket connections: max 10 simultaneous from the same IP
connection_throttle = WebSocketThrottle("ws:connect", rate="10/min")

# Throttle messages within a connection: max 60 messages per minute
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

            # Check the per-message limit on each incoming frame
            await message_throttle(websocket)  # (2)!

            if is_throttled(websocket):  # (3)!
                # Client already got a "rate_limit" JSON notification.
                # Skip processing this message but keep the connection open.
                continue

            # Process the message normally
            response = {"echo": data, "type": "message"}
            await websocket.send_json(response)

    except Exception:
        close_code = 1011
        reason = "Internal error"

    await websocket.close(code=close_code, reason=reason)
```

1. Gate the connection itself. If the connection limit is exceeded *before* the handshake, the connection is rejected with a `HTTP/1.1 429 Too Many Requests`.
2. Call the message throttle directly on every frame.
3. `is_throttled(websocket)` returns `True` if the last throttle call rejected the message. The client already received a JSON notification like `{"type": "rate_limit", "error": "Too many messages", "retry_after": 1}`.

!!! info "What the client sees when throttled"
    When a WebSocket message is rate-limited, Traffik sends the client a structured JSON frame instead of silently dropping the message or closing the connection:

    ```json
    {
      "type": "rate_limit",
      "error": "Too many messages",
      "retry_after": 1
    }
    ```

    This lets clients back off gracefully. If you want different behavior (e.g., immediately close the connection), provide a custom `handle_throttled` handler.

---

## Custom Identifiers

By default, Traffik identifies clients by their IP address. For authenticated APIs, you almost always want to identify by user or API key instead:

```python
from starlette.requests import Request
from traffik import HTTPThrottle, EXEMPTED

# Identify by user ID from a JWT
async def user_identifier(request: Request) -> str:
    # Extract user_id from your auth system
    user_id = request.state.user_id  # Set by your auth middleware
    if user_id is None:
        return request.client.host  # Fall back to IP for anonymous users
    return f"user:{user_id}"

# Identify by API key, and exempt internal services
async def api_key_identifier(request: Request) -> str:
    api_key = request.headers.get("x-api-key", "")
    if api_key.startswith("internal-"):
        return EXEMPTED  # Internal API keys bypass rate limiting
    return f"key:{api_key}"

# Use the custom identifier
throttle = HTTPThrottle(
    "api:user-scoped",
    rate="500/hour",
    identifier=user_identifier,
)
```

!!! tip "The `EXEMPTED` sentinel"
    Returning `EXEMPTED` from an identifier function completely bypasses throttling for that connection. This is the cleanest way to whitelist specific clients, internal services, or admin users - no special-casing needed in your throttle configuration.

---

## What's Next?

You now know the essential patterns. Here's where to go deeper:

- **[Core Concepts](core-concepts/index.md)** - Understand rates, backends, strategies, and identifiers in depth.
- **[Integration Guide](integration/index.md)** - Everything about dependencies, decorators, middleware, and direct calls.
- **[Advanced Features](advanced/index.md)** - Throttle rules, request costs, exemptions, quota contexts, and more.
- **[Error Handling](error-handling.md)** - Circuit breakers, failover, and custom error handlers.
- **[Testing](testing.md)** - How to write tests for throttled endpoints without fighting your own rate limiter.
