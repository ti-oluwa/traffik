# Traffik

Asynchronous distributed rate limiting for FastAPI/Starlette applications.

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)

## Features

- **Fully Asynchronous**: Built for async/await with no blocking operations
- **Distributed**: Atomic operations with distributed locks (Redis, Memcached)
- **Multiple Strategies**: Fixed Window, Sliding Window, Token Bucket, Leaky Bucket
- **HTTP & WebSocket**: Rate limit both protocols
- **Production-Ready**: Circuit breakers, fallback backends, error handling
- **Flexible Integration**: Decorators, dependencies, or middleware
- **Extensible**: Custom strategies, backends, and identifiers

Inspired by [fastapi-limiter](https://github.com/long2ice/fastapi-limiter) but with advanced features for production systems.

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
  - [Rate Format](#rate-format)
  - [Backends](#backends)
  - [Strategies](#strategies)
  - [Identifiers](#identifiers)
- [Integration Methods](#integration-methods)
  - [Dependencies](#dependencies)
  - [Decorators](#decorators)
  - [Middleware](#middleware)
  - [Direct Usage](#direct-usage)
- [Advanced Features](#advanced-features)
  - [Request Costs](#request-costs)
  - [Multiple Limits](#multiple-limits)
  - [Exemptions](#exemptions)
  - [Context-Aware Backends](#context-aware-backends)
- [Error Handling](#error-handling)
- [Custom Strategies](#custom-strategies)
- [Custom Backends](#custom-backends)
- [Testing](#testing)
- [Performance](#performance)
- [API Reference](#api-reference)

## Installation

```bash
# Basic (InMemory backend only)
pip install traffik

# With Redis
pip install "traffik[redis]"

# With Memcached
pip install "traffik[memcached]"

# All backends
pip install "traffik[all]"

# Development
pip install "traffik[dev]"
```

## Quick Start

### Minimal Example

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="api")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="basic", rate="100/minute")

@app.get("/", dependencies=[Depends(throttle)])
async def root():
    return {"message": "ok"}
```

### Example Setup for Production

#### API Routes

```python
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.strategies import SlidingWindowCounterStrategy
from traffik.error_handlers import circuit_breaker_fallback, CircuitBreaker
from traffik.backends.inmemory import InMemoryBackend

# Primary backend
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="prod",
    persistent=True,
)

# Fallback backend
fallback_backend = InMemoryBackend(namespace="fallback")

# Circuit breaker
breaker = CircuitBreaker(
    failure_threshold=10,
    recovery_timeout=60.0,
    success_threshold=3,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with backend(app, persistent=True):
        await fallback_backend.initialize()
        yield
        await fallback_backend.close()

app = FastAPI(lifespan=lifespan)

api_throttle = HTTPThrottle(
    uid="api",
    rate="1000/hour",
    strategy=SlidingWindowCounterStrategy(),
    backend=backend,
    on_error=circuit_breaker_fallback(
        backend=fallback_backend,
        circuit_breaker=breaker,
        max_retries=2,
    ),
)

@app.get("/api/data")
async def get_data(request: Request = Depends(api_throttle)):
    return {"data": "value"}
```

#### Websocket Routes

```python
from contextlib import asynccontextmanager
from traffik import WebSocketThrottle, is_throttled
from traffik.backends.redis import RedisBackend
from traffik.strategies import SlidingWindowCounterStrategy
from traffik.error_handlers import circuit_breaker_fallback, CircuitBreaker
from traffik.backends.inmemory import InMemoryBackend

# Primary backend
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="prod",
    persistent=True,
)

# Fallback backend
fallback_backend = InMemoryBackend(namespace="fallback")

# Circuit breaker
breaker = CircuitBreaker(
    failure_threshold=10,
    recovery_timeout=60.0,
    success_threshold=3,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with backend(app, persistent=True):
        await fallback_backend.initialize()
        yield
        await fallback_backend.close()

app = FastAPI(lifespan=lifespan)

ws_throttle = HTTPThrottle(
    uid="ws",
    rate="1000/hour",
    strategy=SlidingWindowCounterStrategy(),
    backend=backend,
    on_error=circuit_breaker_fallback(
        backend=fallback_backend,
        circuit_breaker=breaker,
        max_retries=2,
    ),
)

@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    close_code = 1000
    reason = "Normal closure"
    while True:
        try:
            data = await websocket.receive_json()
            # Hit throttle. Default handler sends a throttle response if limit reached
            await ws_throttle(websocket)
            # If throttled, do not process further
            if is_throttled(websocket):
                continue
                # Or you could just close the connection with code `1008` here

            # Do something with data...
            await websocket.send_json({"data": "value"})
        except Exception:
            close_code = 1011
            reason = "Internal error"
            break
    await websocket.close(code=close_code, reason=reason)

```

## Core Concepts

### Rate Format

Rate limits are specified as:

- `"<limit>/<unit>"`: e.g., "5/m" means 5 requests per minute
- `"<limit>/<period><unit>"`: e.g., "2/5s" means 2 requests per 5 seconds
- `"<limit>/<period> <unit>"`: e.g., "10/30 seconds" means 10 requests per 30 seconds
- `"<limit> per <period> <unit>"`: e.g., "2 per second" means 2 requests per 1 second.
- `"<limit> per <period><unit>"`: e.g., "2 persecond" means 2 requests per 1 second.
- `Rate` object: for complex periods (e.g., minutes + seconds)

```python
from traffik import HTTPThrottle, Rate

# String format (recommended)
HTTPThrottle(uid="api", rate="100/minute")
HTTPThrottle(uid="api", rate="5/10seconds")
HTTPThrottle(uid="api", rate="1000/500ms")
HTTPThrottle(uid="api", rate="20 per 2 mins")

# Supported units
# ms, millisecond(s)
# s, sec, second(s)
# m, min, minute(s)
# h, hr, hour(s)
# d, day(s)

# Rate object for complex periods
rate = Rate(limit=100, minutes=5, seconds=30)  # 100 per 5.5 minutes
HTTPThrottle(uid="api", rate=rate)

# Unlimited
HTTPThrottle(uid="api", rate="0/0")  # No limits
```

### Backends

Backends store throttle state. Choose based on deployment:

| Backend | Use Case | Persistence | Distributed | Overhead |
|---------|----------|-------------|-------------|----------|
| **InMemory** | Development, testing, single-process | No | No | Minimal |
| **Redis** | Production, multi-instance | Yes | Yes | Low |
| **Memcached** | High-throughput, caching | Yes | Yes | Low |

#### InMemory Backend

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(
    namespace="app",
    persistent=False,  # Don't persist across app restarts
    number_of_shards=16,  # Lock striping for concurrency
    cleanup_frequency=5.0,  # Cleanup expired keys every 5s
)
```

**Characteristics:**

- Lock striping with configurable shards (default: 3)
- Automatic cleanup of expired keys
- Not suitable for multi-process deployments
- Data lost on restart even with `persistent=True`

#### Redis Backend

```python
from traffik.backends.redis import RedisBackend

# From URL
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="app",
    persistent=True,
    lock_type="redis",  # or "redlock" for distributed/multi-cluster redis at the cost of latency
)

# From factory
import redis.asyncio as redis

async def get_redis():
    return await redis.from_url("redis://localhost:6379/0")

backend = RedisBackend(
    connection=get_redis,
    namespace="app",
)
```

**Lock Types:**

- `"redis"`: Single Redis instance, low latency (~0.1ms overhead)
- `"redlock"`: Distributed Redlock algorithm, higher latency (~5ms overhead)

**Characteristics:**

- Lua scripts for atomic operations
- Script caching with NOSCRIPT recovery
- Persistent across restarts
- Production-ready

#### Memcached Backend

```python
from traffik.backends.memcached import MemcachedBackend

backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace="app",
    pool_size=10,
    pool_minsize=2,
    persistent=False,
)
```

**Characteristics:**

- Atomic operations via `add()` and CAS
- Best-effort distributed locks
- No native persistence/non-persistence guarantees. Use `track_keys=True` for better cleanup (at the cost of some latency).
- Good for high-throughput scenarios

**Important:** Memcached has no `KEYS` command. The `clear()` method is a no-op unless `track_keys=True` is enabled (adds overhead).

### Strategies

Strategies determine rate limiting behavior:

| Strategy | Accuracy | Memory | Bursts | Use Case |
|----------|----------|--------|--------|----------|
| **Fixed Window** (default) | Low | O(1) | Yes (2x) | General purpose |
| **Sliding Window Counter** | Medium | O(1) | Reduced | Balanced |
| **Sliding Window Log** | High | O(limit) | No | Financial, security |
| **Token Bucket** | Medium | O(1) | Yes (configurable) | Mobile apps, APIs |
| **Leaky Bucket** | Medium | O(1) | No | Smooth output |

#### Fixed Window (Default)

```python
from traffik.strategies import FixedWindowStrategy

HTTPThrottle(
    uid="api",
    rate="100/minute",
    strategy=FixedWindowStrategy()
)
```

**How it works:**

- Time divided into fixed windows (e.g., 00:00-01:00, 01:00-02:00)
- Counter resets at window boundary
- Can allow up to 2× limit at boundaries (99 at 00:59, 100 at 01:00)

**Storage:**

- `{key}:fixedwindow:counter` - Request count (integer)
- `{key}:fixedwindow:start` - Window start timestamp (only for sub-second windows)

#### Sliding Window Counter

```python
from traffik.strategies import SlidingWindowCounterStrategy

HTTPThrottle(
    uid="api",
    rate="100/minute",
    strategy=SlidingWindowCounterStrategy()
)
```

**How it works:**

- Uses two fixed windows (current + previous)
- Weighted calculation: `(prev_count × overlap%) + current_count`
- At 30s into current window: `overlap = 50%`

**Storage:**

- `{key}:slidingcounter:{window_id}` - Counter for each window
- Two keys active at any time

#### Sliding Window Log

```python
from traffik.strategies import SlidingWindowLogStrategy

HTTPThrottle(
    uid="payment",
    rate="10/minute",
    strategy=SlidingWindowLogStrategy()
)
```

**How it works:**

- Stores timestamp of each request
- Removes timestamps older than window
- Most accurate, no boundary issues

**Storage:**

- `{key}:slidinglog` - Array of `[timestamp, cost]` tuples
- Memory grows with request count (O(limit))

#### Token Bucket

```python
from traffik.strategies import TokenBucketStrategy

HTTPThrottle(
    uid="api",
    rate="100/minute",
    strategy=TokenBucketStrategy(burst_size=150)
)
```

**How it works:**

- Bucket holds tokens which are consumed by requests
- Tokens refill at constant rate
- Each request consumes tokens
- Allows bursts up to `burst_size`. Default `burst_size = rate.limit`

**Storage:**

- `{key}:tokenbucket` - `{"tokens": float, "last_refill": timestamp}`

**With Debt:**

```python
from traffik.strategies import TokenBucketWithDebtStrategy

HTTPThrottle(
    uid="api",
    rate="100/minute",
    strategy=TokenBucketWithDebtStrategy(
        burst_size=150,
        max_debt=50  # Can go to -50 tokens
    )
)
```

Allows temporary overdraft, good for variable traffic.

#### Leaky Bucket

```python
from traffik.strategies import LeakyBucketStrategy

HTTPThrottle(
    uid="external_api",
    rate="50/minute",
    strategy=LeakyBucketStrategy()
)
```

**How it works:**

- Bucket leaks at constant rate
- Requests fill bucket
- No bursts allowed (strictly smooth)

**Storage:**

- `{key}:leakybucket:state` - `{"level": float, "last_leak": timestamp}`

**With Queue:**

```python
from traffik.strategies import LeakyBucketWithQueueStrategy

HTTPThrottle(
    uid="api",
    rate="50/minute",
    strategy=LeakyBucketWithQueueStrategy()
)
```

Maintains FIFO queue of requests with strict ordering.

### Identifiers

Identifiers determine which clients share rate limits:

```python
from starlette.requests import HTTPConnection
from traffik import EXEMPTED

# Default: IP-based
async def default_identifier(connection: HTTPConnection) -> str:
    return get_remote_address(connection) or "__anonymous__"

# User-based
async def user_identifier(connection: HTTPConnection) -> str:
    user_id = extract_from_jwt(connection.headers.get("authorization"))
    return f"user:{user_id}"

# API key-based
async def api_key_identifier(connection: HTTPConnection) -> str:
    api_key = connection.headers.get("x-api-key")
    return f"apikey:{api_key}"

# With exemptions
async def admin_exempt_identifier(connection: HTTPConnection) -> str:
    user = extract_user(connection)
    if user.role == "admin":
        return EXEMPTED  # Bypass rate limiting
    return f"user:{user.id}"

# Usage
HTTPThrottle(
    uid="api",
    rate="100/minute",
    identifier=user_identifier,
)
```

## Integration Methods

### Dependencies

FastAPI dependency injection:

```python
from fastapi import FastAPI, Depends, Request
from traffik import HTTPThrottle

app = FastAPI()
throttle = HTTPThrottle(uid="api", rate="100/minute")

# Single throttle
@app.get("/data", dependencies=[Depends(throttle)])
async def get_data():
    return {"data": "value"}

# Multiple throttles
burst = HTTPThrottle(uid="burst", rate="10/minute")
sustained = HTTPThrottle(uid="sustained", rate="100/hour")

@app.post("/upload", dependencies=[Depends(burst), Depends(sustained)])
async def upload():
    return {"status": "ok"}

# With request access
@app.get("/dynamic")
async def dynamic(request: Request = Depends(throttle)):
    # Request is available
    return {"status": "ok"}
```

### Decorators

FastAPI-only, syntactic sugar over dependencies:

```python
from traffik.decorators import throttled

@app.get("/limited")
@throttled(HTTPThrottle(uid="limited", rate="5/minute"))
async def limited():
    return {"data": "limited"}

# Equivalent to:
# @app.get("/limited", dependencies=[Depends(throttle)])
# Or
# @app.get("/limited")
# async def limited(request: Request = Depends(throttle)):...
```

### Middleware

Apply throttles globally with filtering:

```python
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle

# Basic
api_throttle = HTTPThrottle(uid="api", rate="100/minute")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(api_throttle)
    ],
    backend=backend,
)

# Path-based
admin_throttle = HTTPThrottle(uid="admin", rate="5/minute")
public_throttle = HTTPThrottle(uid="public", rate="1000/minute")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(admin_throttle, path="/admin/"),
        MiddlewareThrottle(public_throttle, path="/api/"),
    ],
)

# Method-based
write_throttle = HTTPThrottle(uid="writes", rate="10/minute")
read_throttle = HTTPThrottle(uid="reads", rate="1000/minute")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(write_throttle, methods={"POST", "PUT", "DELETE"}),
        MiddlewareThrottle(read_throttle, methods={"GET", "HEAD"}),
    ],
)

# Predicate-based
async def is_authenticated(connection: HTTPConnection) -> bool:
    return "authorization" in connection.headers

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(
            HTTPThrottle(uid="auth", rate="200/minute"),
            predicate=is_authenticated
        ),
    ],
)

# Combined
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(
            throttle=HTTPThrottle(uid="complex", rate="25/minute"),
            path="/api/",
            methods={"POST"},
            predicate=is_authenticated,
        ),
    ],
)
```

**Execution order:**

1. Method check (fastest)
2. Path check (regex match)
3. Predicate check (most expensive)

### Direct Usage

Manual throttle invocation:

```python
from starlette.requests import Request

@app.get("/manual")
async def manual_throttle(request: Request):
    # Check and record hit
    await throttle(request)
    
    # Manual cost
    await throttle(request, cost=5)
    
    # With context
    await throttle(request, context={"operation": "export"})
    
    return {"status": "ok"}
```

## Advanced Features

### Request Costs

Different requests can have different impacts:

```python
from traffik import HTTPThrottle

# Fixed cost
expensive_throttle = HTTPThrottle(
    uid="reports",
    rate="100/hour",
    cost=10,  # Each request counts as 10
)

# Dynamic cost
upload_throttle = HTTPThrottle(uid="uploads", rate="1000/hour")

@app.post("/upload")
async def upload(request: Request, file: UploadFile):
    # Cost based on file size (1 per MB)
    file_size_mb = file.size / (1024 * 1024)
    cost = max(1, int(file_size_mb))
    
    await upload_throttle.hit(request, cost=cost)
    return {"status": "uploaded"}

# Cost function
COSTS = {"read": 1, "write": 5, "delete": 10}
async def calculate_cost(connection: HTTPConnection, context: typing.Mapping[str, typing.Any]) -> int:
    operation = context.get("operation", "read")
    return COSTS.get(operation, 1)

dynamic_throttle = HTTPThrottle(
    uid="dynamic",
    rate="100/hour",
    cost=calculate_cost,
)
```

### Multiple Limits

Layer throttles for burst + sustained limits:

```python
# Burst: 10 per minute
burst = HTTPThrottle(uid="burst", rate="10/minute")

# Sustained: 100 per hour
sustained = HTTPThrottle(uid="sustained", rate="100/hour")

@app.get("/data", dependencies=[Depends(burst), Depends(sustained)])
async def get_data():
    return {"data": "value"}
```

### Exemptions

Bypass throttling for specific clients:

```python
from traffik import EXEMPTED

async def premium_identifier(connection: HTTPConnection) -> typing.Any:
    user = extract_user(connection)
    
    # Exempt premium users
    if user.tier == "premium":
        return EXEMPTED
    
    # Exempt based on IP
    if connection.client.host in WHITELISTED_IPS:
        return EXEMPTED
    
    return f"user:{user.id}"

throttle = HTTPThrottle(
    uid="api",
    rate="100/hour",
    identifier=premium_identifier,
)
```

### Context-Aware Backends

Runtime backend selection for multi-tenant applications:

```python
from fastapi import Request

# Enable context-aware backend resolution
api_throttle = HTTPThrottle(
    uid="api",
    rate="1000/hour",
    context_backend=True,  # Resolve backend at runtime
)

# Middleware sets up tenant-specific backend
async def tenant_middleware(request: Request, call_next):
    tenant = extract_tenant(request)
    
    # Select backend based on tenant tier
    if tenant.tier == "enterprise":
        backend = RedisBackend(f"redis://enterprise-{tenant.region}:6379/0")
    elif tenant.tier == "premium":
        backend = RedisBackend(f"redis://premium:6379/{tenant.id}")
    else:
        backend = InMemoryBackend(namespace=f"free:{tenant.id}")
    
    # Execute in backend context
    async with backend(request.app):
        return await call_next(request)

app.middleware("http")(tenant_middleware)

@app.get("/api/data")
async def get_data(request: Request = Depends(api_throttle)):
    return {"data": "tenant-specific"}
```

**When to use:**

- ✅ Multi-tenant SaaS with per-tenant backends
- ✅ A/B testing different strategies
- ✅ Environment-specific backends
- ❌ Simple shared storage (use explicit `backend` parameter)

**Performance impact:** ~1-20ms overhead for backend resolution per reques, depending on backend initialization/connection speed

## Error Handling

Traffik provides comprehensive error handling API for production systems.

### Error Handler Signature

```python
from traffik.throttles import ExceptionInfo
from traffik.types import WaitPeriod

async def error_handler(
    connection: HTTPConnection,
    exc_info: ExceptionInfo,  # Rich exception context
) -> WaitPeriod:  # Return wait time in milliseconds
    """
    exc_info contains:
    - exception: Exception - Exception instance
    - connection: HTTPConnection - HTTP connection
    - cost: int - Request cost
    - rate: Rate - Rate limit configuration
    - backend: ThrottleBackend - Backend that failed
    - throttle: Throttle - Throttle instance
    """
    # Decision logic
    if isinstance(exc_info["exception"], BackendConnectionError):
        return 5000.0  # Fail closed, 5 second wait
    return 0.0  # Allow request
```

### Built-in Error Handlers

#### Basic Handlers (String Literals)

```python
# Development: Allow on errors (fail open)
HTTPThrottle(
    uid="dev",
    rate="100/minute",
    on_error="allow",  # Allow all requests on errors
)

# Production: Throttle on errors (fail closed)
HTTPThrottle(
    uid="prod",
    rate="100/minute",
    on_error="throttle",  # Block all requests on errors (default `min_wait_period` or 1000ms wait, override `min_wait_period` if needed)
)

# Re-raise for external handling
HTTPThrottle(
    uid="api",
    rate="100/minute",
    on_error="raise",  # Let exception propagate
)
```

#### Fallback Backend Handler

Automatic failover to secondary backend:

```python
from traffik.error_handlers import backend_fallback
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend

primary = RedisBackend("redis://primary:6379/0")
fallback = InMemoryBackend(namespace="fallback")

# Initialize fallback during startup
@app.on_event("startup")
async def startup():
    await fallback.initialize()

throttle = HTTPThrottle(
    uid="ha",
    rate="100/minute",
    backend=primary,
    on_error=backend_fallback(
        backend=fallback,
        fallback_on=(BackendConnectionError, TimeoutError),
    ),
)
```

**How it works:**

1. Primary backend fails
2. Attempts throttling with fallback backend
3. If fallback succeeds, returns its wait period
4. If fallback fails, fails closed (`min_wait_period` or 1000ms wait)

#### Circuit Breaker Handler

Prevent cascading failures:

```python
from traffik.error_handlers import circuit_breaker, CircuitBreaker

breaker = CircuitBreaker(
    failure_threshold=5,      # Open after 5 failures
    recovery_timeout=30.0,    # Test recovery after 30s
    success_threshold=2,      # Need 2 successes to close
)

throttle = HTTPThrottle(
    uid="protected",
    rate="100/minute",
    on_error=circuit_breaker(
        circuit_breaker=breaker,
        wait_ms=5000.0,  # Wait 5s when circuit open
    ),
)

# Monitor circuit state
@app.get("/health/circuit")
async def circuit_status():
    return breaker.info()
```

**States:**

- **CLOSED**: Normal operation (failures < threshold)
- **OPEN**: Too many failures (rejects all requests)
- **HALF_OPEN**: Testing recovery (allows limited requests)

**State transitions:**

```
CLOSED ──[5 failures]──> OPEN ──[30s timeout]──> HALF_OPEN
   ↑                                                  |
   └────────────[2 successes]────────────────────────┘
```

#### Retry Handler

Retry transient failures:

```python
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    uid="resilient",
    rate="100/minute",
    on_error=retry(
        max_retries=3,
        retry_delay=0.1,           # 100ms initial delay
        backoff_multiplier=2.0,    # Exponential backoff
        retry_on=(TimeoutError,),  # Only retry timeouts
    ),
)
```

**Retry schedule:**

- Attempt 1: Immediate
- Attempt 2: 100ms delay
- Attempt 3: 200ms delay
- Attempt 4: 400ms delay

#### Degraded Mode Handler

Relaxed limits when backend unavailable:

```python
from traffik.error_handlers import degraded_mode

throttle = HTTPThrottle(
    uid="degradable",
    rate="100/minute",  # Normal: 100/min
    on_error=degraded_mode(
        rate_multiplier=3.0,  # Degraded: ~33/min (3× wait)
        duration=300.0,        # Stay degraded for 5 minutes
    ),
)
```

Provides basic protection without backend access.

#### Smart Fallback Handler (Production)

Combines circuit breaker + retry + fallback:

```python
from traffik.error_handlers import circuit_breaker_fallback, CircuitBreaker
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend

primary = RedisBackend("redis://primary:6379/0")
fallback = InMemoryBackend(namespace="fallback")

breaker = CircuitBreaker(
    failure_threshold=10,
    recovery_timeout=60.0,
    success_threshold=3,
)

throttle = HTTPThrottle(
    uid="production",
    rate="1000/hour",
    backend=primary,
    on_error=circuit_breaker_fallback(
        backend=fallback,
        circuit_breaker=breaker,
        max_retries=2,
    ),
)
```

**Decision flow:**

```markdown
Error occurs
    ↓
Circuit open? ──Yes──> Use fallback backend
    ↓ No
Retry primary (max 2) ──Success──> Return ✓
    ↓ Fail
Record failure ──> Use fallback backend
```

**Performance:**

- Circuit check: ~0.1µs
- Retry: ~100-400ms (depends on retries)
- Fallback: ~1-5ms (one backend operation)

### Custom Error Handlers

```python
import logging
from traffik.throttles import ExceptionInfo
from traffik.types import WaitPeriod

logger = logging.getLogger(__name__)

async def handle_errors(
    connection: HTTPConnection, exc_info: ExceptionInfo
) -> WaitPeriod:
    exc = exc_info["exception"]
    backend_type = type(exc_info["backend"]).__name__
    
    # Log error
    logger.error(
        f"Throttle error: {exc.__class__.__name__} on {backend_type}",
        extra={
            "path": connection.url.path,
            "rate": str(exc_info["rate"]),
            "cost": exc_info["cost"],
        },
    )
    
    # Decision based on error type
    if isinstance(exc, BackendConnectionError):
        return 5000.0  # Fail closed for connection errors
    elif isinstance(exc, TimeoutError):
        return 0.0  # Allow for timeouts
    return 1000.0  # Default: 1s wait

throttle = HTTPThrottle(
    uid="custom",
    rate="100/minute",
    on_error=handle_errors,
)
```

### Error Handler Selection Guide

| Scenario | Handler | Reasoning |
|----------|---------|-----------|
| Development | `"allow"` | Never block developers |
| Security-critical | `"throttle"` | Fail closed always |
| High-availability | `circuit_breaker_fallback` | Best resilience |
| Multi-region | `backend_fallback` | Automatic failover |
| Unstable backend | `circuit_breaker` | Prevent cascades |
| Network issues | `retry` | Handle transient errors |
| Observability | Custom handler | Logging + metrics |

### Backend-Level Error Handling

Backends also support global `on_error` handlers. For throttle-specific handling, use throttle `on_error`.

```python
# All throttles using this backend inherit its `on_error` behavior
# except specified otherwise
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    on_error="throttle",  # "allow", "throttle", or "raise"
)
```

Throttle `on_error` takes precedence over backend `on_error`.

## Custom Strategies

Implement custom rate limiting algorithms:

```python
from dataclasses import dataclass
from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod
from traffik.utils import time

@dataclass(frozen=True)
class CustomStrategy:
    """Custom rate limiting strategy."""
    
    param1: int = 10
    param2: float = 0.5
    
    async def __call__(
        self,
        key: Stringable,
        rate: Rate,
        backend: ThrottleBackend,
        cost: int = 1,
    ) -> WaitPeriod:
        """
        Apply rate limiting.
        
        :return: Wait time in milliseconds (0.0 if allowed)
        """
        if rate.unlimited:
            return 0.0
        
        now = time() * 1000
        full_key = backend.get_key(str(key))
        counter_key = f"{full_key}:custom:counter"
        ttl_seconds = int(rate.expire // 1000) + 1
        
        # Use locks for atomicity (mostly needed for multi-step logic)
        # Lock is overkill for single increment, but shown here for completeness
        async with await backend.lock(
            f"lock:{counter_key}",
            blocking=True,
            blocking_timeout=1.0,
        ):
            # Your logic here
            count = await backend.increment_with_ttl(
                counter_key,
                amount=cost,
                ttl=ttl_seconds,
            )
            if count > rate.limit:
                # Calculate wait time
                return rate.expire  # Simplified
            return 0.0

# Usage
throttle = HTTPThrottle(
    uid="api",
    rate="100/minute",
    strategy=CustomStrategy(param1=20),
)
```

**Best practices:**

1. Always use locks for multi-step operations
2. Set TTLs to prevent memory leaks
3. Handle `rate.unlimited` early
4. Return milliseconds, not seconds
5. Strategy configuration should not be mutable/changed at runtime. You can use dataclasses with `frozen=True`
6. Avoid blocking operations (no logging in hot paths)

## Custom Backends

Implement custom storage backends:

```python
import typing
from traffik.backends.base import ThrottleBackend
from traffik.types import AsyncLock, HTTPConnectionT

class CustomBackend(ThrottleBackend[YourConnectionType, HTTPConnectionT]):
    """Custom backend implementation."""
    
    async def initialize(self) -> None:
        """Setup connection/resources."""
        self.connection = await create_connection()
    
    async def get(self, key: str) -> typing.Optional[str]:
        """Get value for key."""
        return await self.connection.get(key)
    
    async def set(
        self,
        key: str,
        value: str,
        expire: typing.Optional[int] = None
    ) -> None:
        """Set value with optional TTL in seconds."""
        await self.connection.set(key, value, ttl=expire)
    
    async def delete(self, key: str) -> bool:
        """Delete key. Return True if deleted."""
        return await self.connection.delete(key)
    
    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment counter. Return new value."""
        return await self.connection.incr(key, amount)
    
    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement counter. Return new value."""
        return await self.connection.decr(key, amount)
    
    async def expire(self, key: str, seconds: int) -> bool:
        """Set TTL on existing key. Return True if set."""
        return await self.connection.expire(key, seconds)
    
    async def increment_with_ttl(
        self,
        key: str,
        amount: int = 1,
        ttl: int = 60
    ) -> int:
        """
        Atomically increment and set TTL if key is new.
        Return new value.
        """
        # Default implementation (override for better performance)
        value = await self.increment(key, amount)
        if value == amount:  # New key
            await self.expire(key, ttl)
        return value
    
    async def multi_get(
        self,
        *keys: str
    ) -> typing.List[typing.Optional[str]]:
        """
        Get multiple keys atomically.
        Return values in same order as keys.
        """
        return [await self.get(key) for key in keys]
    
    async def get_lock(self, name: str) -> AsyncLock:
        """Get distributed lock."""
        return YourLockImplementation(name, self.connection)
    
    async def reset(self) -> None:
        """Clear all data."""
        await self.connection.flush()
    
    async def close(self) -> None:
        """Cleanup resources."""
        await self.connection.close()
        self.connection = None
```

**Required methods:**

- `initialize()`, `get()`, `set()`, `delete()`
- `increment()`, `decrement()`, `expire()`
- `get_lock()`, `reset()`, `close()`

**Performance-critical:**

- `increment_with_ttl()` - Override for atomic operation
- `multi_get()` - Override for batch retrieval
- All operations must be non-blocking and fast

## Testing

### Basic Test

```python
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

@pytest.fixture
async def backend():
    backend = InMemoryBackend(namespace="test", persistent=False)
    async with backend(close_on_exit=True):
        yield backend

@pytest.mark.anyio
async def test_throttling(backend):
    app = FastAPI(lifespan=backend.lifespan)
    throttle = HTTPThrottle(uid="test", rate="2/second")
    
    @app.get("/", dependencies=[Depends(throttle)])
    async def root():
        return {"ok": True}
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # First 2 requests succeed
        r1 = await client.get("/")
        r2 = await client.get("/")
        assert r1.status_code == 200
        assert r2.status_code == 200
        
        # Third request throttled
        r3 = await client.get("/")
        assert r3.status_code == 429
        assert "retry-after" in r3.headers
```

### Strategy Testing

```python
@pytest.mark.parametrize("strategy", [
    FixedWindowStrategy(),
    SlidingWindowCounterStrategy(),
    SlidingWindowLogStrategy(),
    TokenBucketStrategy(),
])
@pytest.mark.anyio
async def test_strategy(backend, strategy):
    throttle = HTTPThrottle(
        uid="test",
        rate="5/second",
        strategy=strategy,
    )
    app = FastAPI(lifespan=backend.lifespan)
    
    @app.get("/", dependencies=[Depends(throttle)])
    async def root():
        return {"ok": True}
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        # Test logic
        ...
```

### Error Handler Testing

```python
@pytest.mark.anyio
async def test_fallback_handler():
    primary = RedisBackend("redis://localhost:6379/999")  # Bad DB
    fallback = InMemoryBackend(namespace="fallback")
    await fallback.initialize()
    
    throttle = HTTPThrottle(
        uid="test",
        rate="5/second",
        backend=primary,
        on_error=backend_fallback(fallback),
    )
    
    app = FastAPI()
    
    @app.get("/", dependencies=[Depends(throttle)])
    async def root():
        return {"ok": True}
    
    # Should use fallback when primary fails
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        response = await client.get("/")
        assert response.status_code == 200
```

### Docker Testing

```bash
# Run full test suite
./docker-test.sh test

# Fast tests only
./docker-test.sh test-fast

# Test across Python versions
./docker-test.sh test-matrix

# Development environment
./docker-test.sh dev
```

See [DOCKER.md](DOCKER.md) and [TESTING.md](TESTING.md) for details.

## Performance

### Benchmark Results

Single-process benchmarks (Redis backend, 100 req/min limit):

| Operation | Latency (P50) | Latency (P95) | RPS |
|-----------|---------------|---------------|-----|
| Allow (under limit) | 0.95ms | 1.72ms | 880+ |
| Throttle (over limit) | 1.00ms | 1.31ms | 950+ |
| InMemory backend | 0.50ms | 1.00ms | 1200+ |

### Optimization Tips

1. **Use appropriate backend:**
   - InMemory: Single-process, lowest latency
   - Redis: Distributed, good performance
   - Memcached: High-throughput scenarios

2. **Choose strategy wisely:**
   - Fixed Window: Fastest (O(1) memory, minimal operations)
   - Sliding Window Counter: Good balance
   - Sliding Window Log: Slowest (O(limit) memory)

3. **Configure lock striping:**

   ```python
   InMemoryBackend(number_of_shards=32)  # More shards = better concurrency
   ```

4. **Use connection pooling:**

   ```python
   MemcachedBackend(pool_size=20)  # Larger pool for high concurrency
   ```

5. **Minimize identifier complexity:**

   ```python
   # Fast
   async def simple_identifier(conn):
       return conn.client.host
   
   # Slow (avoid in hot paths)
   async def complex_identifier(conn):
       user = await db.get_user(extract_jwt(conn))  # DB query!
       return f"user:{user.id}"
   ```

6. **Avoid logging in backend operations:** Logging can cause ~10× slowdown**. Especially when the logging backend is slow (e.g., file I/O).

## API Reference

### Throttle Classes

#### `HTTPThrottle`

```python
HTTPThrottle(
    uid: str,
    rate: Union[Rate, str, RateFunc],
    identifier: Optional[ConnectionIdentifier] = None,
    handle_throttled: Optional[ConnectionThrottledHandler] = None,
    strategy: Optional[ThrottleStrategy] = None,
    backend: Optional[ThrottleBackend] = None,
    cost: Union[int, CostFunc] = 1,
    context_backend: bool = False,
    min_wait_period: Optional[int] = None,
    headers: Optional[Mapping[str, str]] = None,
    on_error: Union[Literal["allow", "throttle", "raise"], ErrorHandler] = None,
)
```

**Parameters:**

- `uid`: Unique identifier for this throttle
- `rate`: Rate limit (`"100/minute"` or `Rate` object or function)
- `identifier`: Client identifier function (default: IP-based)
- `handle_throttled`: Custom throttled response handler
- `strategy`: Rate limiting strategy (default: `FixedWindowStrategy`)
- `backend`: Storage backend (default: from app context)
- `cost`: Request cost (default: 1 or function)
- `context_backend`: Enable runtime backend resolution (default: False)
- `min_wait_period`: Minimum wait time in milliseconds
- `headers`: Extra headers for throttled responses
- `on_error`: Error handling strategy

#### `WebSocketThrottle`

Same parameters as `HTTPThrottle`, but for WebSocket connections.

### Rate

```python
Rate(
    limit: int,
    milliseconds: int = 0,
    seconds: int = 0,
    minutes: int = 0,
    hours: int = 0,
)

# Properties
rate.limit          # int
rate.expire         # milliseconds (total period)
rate.unlimited      # bool
rate.is_subsecond   # bool (< 1 second)
rate.rps            # requests per second
rate.rpm            # requests per minute
rate.rph            # requests per hour
rate.rpd            # requests per day

# Class methods
Rate.parse("100/minute") -> Rate
```

### Backends

All backends inherit from `ThrottleBackend[T, HTTPConnectionT]`:

```python
class ThrottleBackend:
    async def initialize() -> None
    async def ready() -> bool
    async def get(key: str) -> Optional[str]
    async def set(key: str, value: str, expire: Optional[int]) -> None
    async def delete(key: str) -> bool
    async def increment(key: str, amount: int = 1) -> int
    async def decrement(key: str, amount: int = 1) -> int
    async def expire(key: str, seconds: int) -> bool
    async def increment_with_ttl(key: str, amount: int, ttl: int) -> int
    async def multi_get(*keys: str) -> List[Optional[str]]
    async def get_lock(name: str) -> AsyncLock
    async def reset() -> None
    async def close() -> None
    
# Context manager
async with backend(app, persistent=True, close_on_exit=True):
    ...
```

### Strategies

All strategies implement:

```python
async def __call__(
    key: Stringable,
    rate: Rate,
    backend: ThrottleBackend,
    cost: int = 1,
) -> WaitPeriod:
    ...
```

Available strategies:

- `FixedWindowStrategy()`
- `SlidingWindowCounterStrategy()`
- `SlidingWindowLogStrategy()`
- `TokenBucketStrategy(burst_size: Optional[int] = None)`
- `TokenBucketWithDebtStrategy(burst_size: Optional[int], max_debt: int)`
- `LeakyBucketStrategy()`
- `LeakyBucketWithQueueStrategy()`

### Middleware

```python
MiddlewareThrottle(
    throttle: Throttle,
    path: Optional[Union[str, Pattern]] = None,
    methods: Optional[Set[str]] = None,
    predicate: Optional[Callable[[HTTPConnection], Awaitable[bool]]] = None,
)

ThrottleMiddleware(
    app: ASGIApp,
    middleware_throttles: Sequence[MiddlewareThrottle],
    backend: Optional[ThrottleBackend] = None,
)
```

### Exceptions

```python
from traffik.exceptions import (
    TraffikException,          # Base
    ConfigurationError,        # Invalid config
    ConnectionThrottled,       # Rate limit exceeded (HTTP 429)
    BackendError,              # Backend operation failed
    BackendConnectionError,    # Backend connection failed
    LockTimeoutError,          # Lock acquisition timeout
)
```

### Error Handlers

```python
from traffik.error_handlers import (
    backend_fallback,
    circuit_breaker,
    retry,
    degraded_mode,
    circuit_breaker_fallback,
    throttle_fallbacks,
    CircuitBreaker,
)

# String literal handlers (built-in)
on_error="allow"      # Allow all requests on errors
on_error="throttle"   # Throttle all requests on errors (`min_wait_period` or 1000ms wait if set)
on_error="raise"      # Re-raise exceptions
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT License - see [LICENSE](LICENSE) file.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.
