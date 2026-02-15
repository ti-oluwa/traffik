# Error Handling

Rate limiting backends can fail. Redis goes down. Network blips happen. Memcached runs out of memory. The question isn't *if* your backend will fail — it's *what should Traffik do when it does*?

Traffik gives you fine-grained control over that answer: from a simple one-liner to a full circuit-breaker pattern with automatic failover.

---

## The Error Handler Signature

A custom error handler is an async function that receives the connection and exception info, and returns a `WaitPeriod` (milliseconds):

```python
from traffik.throttles import ThrottleExceptionInfo
from traffik.types import WaitPeriod

async def my_handler(
    connection,                  # Request or WebSocket
    exc_info: ThrottleExceptionInfo,
) -> WaitPeriod:
    # Return 0 to allow the request through (fail open)
    # Return a positive number to throttle the client
    # Raise to propagate the exception
    return 0
```

### `ThrottleExceptionInfo` Fields

| Field | Type | Description |
|---|---|---|
| `exception` | `Exception` | The exception that was raised by the backend |
| `connection` | `HTTPConnection` | The current HTTP connection |
| `cost` | `int` | The cost of the throttle operation that failed |
| `rate` | `Rate` | The rate limit definition |
| `backend` | `ThrottleBackend` | The backend that raised the error |
| `context` | `dict \| None` | The throttle context at the time of the error |
| `throttle` | `Throttle` | The throttle instance that triggered the operation |

---

## String Shortcuts

For simple cases, skip the function entirely and use one of three string values:

```python
from traffik import HTTPThrottle

# Fail open: backend error -> allow the request through
throttle = HTTPThrottle("api", rate="100/min", on_error="allow")

# Fail closed: backend error -> throttle the client as if limit exceeded
throttle = HTTPThrottle("api", rate="100/min", on_error="throttle")

# Propagate: backend error -> raise the exception (crashes the request)
throttle = HTTPThrottle("api", rate="100/min", on_error="raise")
```

`"throttle"` is the default behavior — if you don't set `on_error`, Traffik fails closed. Better to slow things down than to let unlimited traffic through when your backend is struggling.

---

## `backend_fallback` — Automatic Failover

Switch to a backup backend when the primary fails:

```python
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import backend_fallback
from traffik.exceptions import BackendError

primary = RedisBackend("redis://primary:6379", namespace="myapp")
fallback = InMemoryBackend(namespace="myapp-fallback")

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    backend=primary,
    on_error=backend_fallback(
        backend=fallback,
        fallback_on=(BackendError, TimeoutError),
    )
)
```

**How it works:**

1. Primary backend raises an exception
2. Handler checks if the exception type is in `fallback_on`
3. If yes: attempts the throttle operation on the fallback backend
4. If the fallback also fails: re-raises the exception
5. If no: re-raises the original exception immediately

!!! tip "InMemory as fallback"
    `InMemoryBackend` makes an excellent fallback for Redis/Memcached. It's always available (no network), starts up instantly, and provides reasonable throttling even without distributed coordination. In a single-node failure scenario, per-process rate limiting is usually better than no rate limiting.

---

## `retry` — Retry Transient Failures

Retry the throttle operation with backoff before giving up:

```python
from traffik import HTTPThrottle
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    on_error=retry(
        max_retries=3,
        retry_delay=0.1,             # Start with 100ms
        backoff_multiplier=2.0,      # Double each retry: 100ms, 200ms, 400ms
        retry_on=(TimeoutError,),    # Only retry timeouts, not config errors
    )
)
```

**How it works:**

1. Exception is raised during throttling
2. If not in `retry_on`: re-raise immediately
3. If in `retry_on`: wait `retry_delay` seconds and retry
4. Apply `backoff_multiplier` to delay for subsequent retries
5. If all retries fail: re-raise the last exception

---

## `failover` — Full Circuit Breaker + Retry + Fallback

The recommended pattern for production. Combines all resilience techniques:

```python
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.error_handlers import failover, CircuitBreaker

primary = RedisBackend("redis://primary:6379", namespace="myapp")
fallback = InMemoryBackend(namespace="myapp-fallback")

breaker = CircuitBreaker(
    failure_threshold=5,      # Open after 5 consecutive failures
    recovery_timeout=30.0,    # Try to recover after 30 seconds
    success_threshold=2,      # Close after 2 consecutive successes in half-open
)

throttle = HTTPThrottle(
    "api",
    rate="100/min",
    backend=primary,
    on_error=failover(
        backend=fallback,
        breaker=breaker,
        max_retries=2,
        retry_delay=0.05,
    )
)
```

**How it works:**

| Circuit State | Behavior |
|---|---|
| `CLOSED` | Normal: retry primary up to `max_retries` times, then fall back |
| `OPEN` | Degraded: skip primary entirely, use fallback immediately |
| `HALF_OPEN` | Testing: allow one request through primary; success closes circuit, failure reopens |

**State transitions:**

- `CLOSED` → `OPEN`: `failure_threshold` consecutive failures
- `OPEN` → `HALF_OPEN`: after `recovery_timeout` seconds
- `HALF_OPEN` → `CLOSED`: `success_threshold` consecutive successes
- `HALF_OPEN` → `OPEN`: any failure during recovery test

---

## `CircuitBreaker` Reference

```python
from traffik.error_handlers import CircuitBreaker

breaker = CircuitBreaker(
    failure_threshold=5,    # Failures before opening (default: 5)
    recovery_timeout=60.0,  # Seconds before half-open attempt (default: 60.0)
    success_threshold=2,    # Successes in half-open to close (default: 2)
)

# Inspect state
info = breaker.info()
# {
#     "state": "closed",   # "closed", "open", or "half_open"
#     "failures": 0,
#     "successes": 0,
#     "opened_at": None,   # datetime when circuit opened, or None
# }
```

---

## Custom Error Handler with Logging

```python
import logging
from traffik import HTTPThrottle
from traffik.types import WaitPeriod
from traffik.throttles import ThrottleExceptionInfo
from traffik.exceptions import BackendError

logger = logging.getLogger("traffik.errors")


async def logged_fallback_handler(connection, exc_info: ThrottleExceptionInfo) -> WaitPeriod:
    exc = exc_info["exception"]
    throttle = exc_info["throttle"]

    if isinstance(exc, BackendError):
        logger.warning(
            "Backend error on throttle %r: %s. Allowing request through.",
            throttle.uid,
            exc,
        )
        return 0  # Fail open: allow the request

    logger.error(
        "Unexpected error on throttle %r: %s. Throttling client.",
        throttle.uid,
        exc,
        exc_info=True,
    )
    return 1000  # Fail closed: throttle for 1 second


throttle = HTTPThrottle(
    "api",
    rate="100/min",
    on_error=logged_fallback_handler,
)
```

!!! warning "Don't log inside backends"
    Logging inside backend operations (not error handlers) causes serious performance degradation. Logging is synchronous and can block the event loop. See [Performance Tips](performance.md) for details. Logging inside error *handlers* is fine — handlers only run when something goes wrong.

---

## Error Handler Selection Guide

| Scenario | Handler to Use |
|---|---|
| Development / low stakes | `on_error="allow"` — fail open, keep dev experience smooth |
| Security-sensitive API | `on_error="throttle"` — fail closed, protect the resource |
| Debugging backend issues | `on_error="raise"` — propagate exceptions for visibility |
| Redis down, InMemory fallback | `backend_fallback(fallback_backend)` |
| Transient network blips | `retry(max_retries=3, retry_on=(TimeoutError,))` |
| Production with HA requirements | `failover(fallback, breaker=CircuitBreaker(...))` |
| Custom logic + logging | Custom async function |

---

## Backend-Level vs. Throttle-Level Handlers

You can set `on_error` at the backend level — it applies to all throttles using that backend:

```python
backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    on_error="allow",   # Backend-level default: fail open
)

# This throttle inherits the backend's "allow" behavior
read_throttle = HTTPThrottle("api:read", rate="1000/min", backend=backend)

# This throttle overrides with its own handler (takes precedence)
write_throttle = HTTPThrottle(
    "api:write",
    rate="100/min",
    backend=backend,
    on_error="throttle",   # Throttle-level: fail closed for writes
)
```

Throttle-level `on_error` always takes precedence over backend-level `on_error`.

---

## `ConnectionThrottled`

When a client exceeds the rate limit, Traffik raises `ConnectionThrottled`. This is a subclass of Starlette's `HTTPException`, which means FastAPI and Starlette handle it automatically — no custom handler registration is needed for basic behavior. The client receives a `429 Too Many Requests` response without any extra setup on your part.

```python
from traffik.exceptions import ConnectionThrottled
```

### Custom handler for richer responses

If you want to return a richer response — for example, including `Retry-After` information in the response body or a custom JSON structure — register a custom exception handler:

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from traffik.exceptions import ConnectionThrottled

app = FastAPI()

async def throttle_handler(request: Request, exc: ConnectionThrottled) -> JSONResponse:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": exc.detail,
            "retry_after": retry_after,
        },
        headers=exc.headers or {},
    )

app.add_exception_handler(ConnectionThrottled, throttle_handler)
```

### Why prefer `ConnectionThrottled` over plain `HTTPException`?

When writing throttle-related code — custom handlers, middleware, or decorators — use `ConnectionThrottled` rather than a plain `HTTPException(status_code=429)`. It carries the same automatic handling, but conveys clearer semantics: this is a rate-limit error specifically, not just any 429. It also makes your exception handlers easier to scope precisely.

```python
# Prefer this in throttle-related code:
raise ConnectionThrottled(detail="Too many requests. Please slow down.")

# Rather than the generic form:
# raise HTTPException(status_code=429, detail="Too many requests.")
```
