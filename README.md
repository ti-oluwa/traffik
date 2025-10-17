# Traffik - A Starlette throttling library

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
<!-- [![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/) -->

Traffik provides flexible, production-ready rate limiting for Starlette and FastAPI applications with support for both HTTP and WebSocket connections. It offers multiple rate limiting strategies including Fixed Window, Sliding Window, Token Bucket, and Leaky Bucket algorithms, allowing you to choose the approach that best fits your use case.

The library features pluggable backends (in-memory, Redis, Memcached), dynamic backend resolution for multi-tenant applications, and comprehensive testing across concurrent scenarios. Whether you need simple per-endpoint limits or complex distributed rate limiting, Traffik provides the flexibility and robustness to handle your requirements.

Traffik was inspired by [fastapi-limiter](https://github.com/long2ice/fastapi-limiter) but has evolved into a more comprehensive solution with advanced features like multiple strategies, atomic operations, and extensive concurrency support.

## Features

- 🚀 **Easy Integration**: Decorator, dependency, and middleware-based throttling
- 🎯 **Multiple Strategies**: Fixed Window (default), Sliding Window, Token Bucket, Leaky Bucket
- 🔄 **Multiple Backends**: In-memory, Redis, Memcached with atomic operation support
- 🌐 **Protocol Support**: Both HTTP and WebSocket throttling
- 🏢 **Dynamic Backend Resolution**: Multi-tenant support with runtime backend switching
- 🔧 **Flexible Rate Configuration**: Simple string format ("100/m") or Rate objects
- 📊 **Smart Client Identification**: IP-based by default, fully customizable
- 🛡️ **Production-Ready**: Distributed locks, proper concurrency handling
- 🧪 **Thoroughly Tested**: Comprehensive test suite covering concurrency and multithreading

## Rate Limiting Strategies

Traffik supports multiple rate limiting strategies, each with different trade-offs. The **Fixed Window strategy is used by default** for its simplicity and performance.

### Fixed Window (Default)

Divides time into fixed windows and counts requests within each window. Simple, fast, and memory-efficient.

**Pros:** Simple, constant memory, fast  
**Cons:** Can allow bursts at window boundaries (up to 2x limit)

```python
from traffik.strategies import FixedWindowStrategy

throttle = HTTPThrottle(
    uid="api_limit",
    rate="100/m",
    strategy=FixedWindowStrategy()  # Default, can be omitted
)
```

### Sliding Window

Most accurate rate limiting with continuous sliding window evaluation. Prevents boundary exploitation.

**Pros:** Most accurate, no boundary issues  
**Cons:** Higher memory usage (O(limit) per key)

```python
from traffik.strategies import SlidingWindowLogStrategy

throttle = HTTPThrottle(
    uid="payment_api",
    rate="10/m",
    strategy=SlidingWindowLogStrategy()
)
```

### Token Bucket

Allows controlled bursts while maintaining average rate over time. Tokens refill continuously.

**Pros:** Allows bursts, smooth distribution, self-recovering  
**Cons:** Slightly more complex

```python
from traffik.strategies import TokenBucketStrategy

throttle = HTTPThrottle(
    uid="user_api",
    rate="100/m",
    strategy=TokenBucketStrategy(burst_size=150)  # Allow bursts up to 150
)
```

### Leaky Bucket

Enforces perfectly smooth traffic output. No bursts allowed.

**Pros:** Smooth output, protects downstream services  
**Cons:** Less forgiving, may reject legitimate bursts

```python
from traffik.strategies import LeakyBucketStrategy

throttle = HTTPThrottle(
    uid="third_party_api",
    rate="50/m",
    strategy=LeakyBucketStrategy()
)
```

## Installation

We recommend using `uv`, however, it is not a strict requirement.

### Install `uv` (optional)

Visit the [uv documentation](https://docs.astral.sh/uv/getting-started/installation/) for installation instructions.

### Basic Installation

```bash
uv add traffik

# or using pip
pip install traffik
```

Install with FastAPI support:

```bash
uv add "traffik[fastapi]"

# or using pip
pip install "traffik[fastapi]"
```

### With Redis Backend

```bash
uv add "traffik[redis]"

# or using pip
pip install "traffik[redis]"
```

### With Memcached Backend

```bash
uv add "traffik[memcached]"

# or using pip
pip install "traffik[memcached]"
```

### With All Features

```bash
uv add "traffik[all]"

# or using pip
pip install "traffik[all]"
```

### Development Installation

```bash
git clone https://github.com/your-username/traffik.git
cd traffik

uv sync --extra dev

# or using pip
pip install -e .[dev]
```

## Quick Testing with Docker

For quick testing across different platforms and Python versions:

```bash
# Run fast tests
./docker-test.sh test-fast

# Run full test suite
./docker-test.sh test

# Start development environment
./docker-test.sh dev

# Test across Python versions
./docker-test.sh test-matrix
```

**Testing Documentation:**

- [DOCKER.md](DOCKER.md) - Complete Docker testing guide
- [TESTING.md](TESTING.md) - Quick testing guide  
- [TESTING_COMPLETE.md](TESTING_COMPLETE.md) - Comprehensive testing reference

## Quick Start

### 1. Basic HTTP Throttling with Starlette

```python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

# Create backend
backend = InMemoryBackend(namespace="myapp", persistent=False)

# Create throttle with string rate format
throttle = HTTPThrottle(
    uid="basic_limit",
    rate="5/10s",  # 5 requests per 10 seconds
)

async def throttled_endpoint(request: Request):
    await throttle(request)
    return JSONResponse({"message": "Success"})

app = Starlette(
    routes=[
        Route("/api/data", throttled_endpoint, methods=["GET"]),
    ],
    lifespan=backend.lifespan,
)
```

### 2. FastAPI with Dependency Injection

```python
from fastapi import FastAPI, Depends
from contextlib import asynccontextmanager
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

# Create backend
backend = InMemoryBackend(namespace="api")

# Setup lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with backend(app, persistent=True, close_on_exit=True):
        yield

app = FastAPI(lifespan=lifespan)

# Create throttle
throttle = HTTPThrottle(uid="endpoint_limit", rate="10/m")

@app.get("/api/hello", dependencies=[Depends(throttle)])
async def say_hello():
    return {"message": "Hello World"}
```

### 3. Using Decorators (FastAPI Only)

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from traffik.decorators import throttled
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="api",
    persistent=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with backend(app):
        yield

app = FastAPI(lifespan=lifespan)

@app.get("/api/limited")
@throttled(HTTPThrottle(uid="limited", rate="5/m"))
async def limited_endpoint():
    return {"data": "Limited access"}
```

### 4. WebSocket Throttling

```python
from traffik.throttles import WebSocketThrottle
from starlette.websockets import WebSocket
from starlette.exceptions import HTTPException

ws_throttle = WebSocketThrottle(uid="ws_messages", rate="3/10s")

async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    
    while True:
        try:
            data = await websocket.receive_json()
            await ws_throttle(websocket)  # Rate limit per message
            
            await websocket.send_json({
                "status": "success",
                "data": data,
            })
        except HTTPException as exc:
            await websocket.send_json({
                "status": "error", 
                "status_code": exc.status_code,
                "detail": exc.detail,
            })
            break
    
    await websocket.close()
```

## Backends

Traffik provides three backend implementations with full atomic operation support and distributed locking.

### In-Memory Backend

Perfect for development, testing, and single-instance applications:

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(
    namespace="myapp",
    persistent=False,  # Don't persist across restarts
)
```

**Pros:**

- No external dependencies
- Fast and simple
- Great for development and testing

**Cons:**

- Not suitable for multi-process/distributed systems
- Data lost on restart (even with `persistent=True`)

### Redis Backend

Recommended for production with distributed systems:

```python
from traffik.backends.redis import RedisBackend

# From connection string
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="myapp",
    persistent=True,
)

# From Redis client factory
import redis.asyncio as redis

def get_client() -> redis.Redis:
    return redis.Redis(host='localhost', port=6379, db=0)

backend = RedisBackend(
    connection=get_client,
    namespace="myapp",
)
```

**Features:**

- Distributed locks using Redlock algorithm
- Production-ready persistence

**Pros:**

- Multi-process and distributed support
- Persistence across restarts
- Battle-tested for production

**Cons:**

- Requires Redis server
- Additional infrastructure

### Memcached Backend

Lightweight distributed caching solution:

```python
from traffik.backends.memcached import MemcachedBackend

backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace="myapp",
    pool_size=10,
)
```

**Features:**

- Lightweight distributed locks
- Atomic operations via CAS (Compare-And-Swap)
- Connection pooling
- Fast in-memory storage

**Pros:**

- Lightweight and fast
- Good for high-throughput scenarios
- Simple deployment

**Cons:**

- Less feature-rich than Redis

### Custom Backends

Create custom backends by subclassing `ThrottleBackend`:

```python
from traffik.backends.base import ThrottleBackend
from traffik.types import HTTPConnectionT
import typing

class CustomBackend(ThrottleBackend[typing.Dict, HTTPConnectionT]):
    """Custom backend with your storage solution"""
    
    async def initialize(self) -> None:
        """Setup connection/resources"""
        pass
    
    async def get(self, key: str) -> typing.Optional[str]:
        """Get value for key"""
        pass
    
    async def set(self, key: str, value: str, expire: typing.Optional[int] = None) -> None:
        """Set value with optional TTL"""
        pass
    
    async def delete(self, key: str) -> None:
        """Delete key"""
        pass
    
    async def increment(self, key: str, amount: int = 1) -> int:
        """Atomically increment counter"""
        pass
    
    async def decrement(self, key: str, amount: int = 1) -> int:
        """Atomically decrement counter"""
        pass
    
    async def increment_with_ttl(self, key: str, amount: int, ttl: int) -> int:
        """Atomically increment with TTL set"""
        pass
    
    async def lock(self, key: str, blocking: bool = True, blocking_timeout: typing.Optional[float] = None):
        """Get distributed lock"""
        pass
    
    async def reset(self) -> None:
        """Clear all data"""
        pass
    
    async def close(self) -> None:
        """Cleanup resources"""
        pass
```

## Configuration Options

### Rate Format

Traffik supports flexible rate specification using string format or `Rate` objects:

```python
from traffik import Rate
from traffik.throttles import HTTPThrottle

# String format (recommended)
throttle = HTTPThrottle(uid="api_v1", rate="100/m")  # 100 per minute
throttle = HTTPThrottle(uid="api_v2", rate="5/10s")  # 5 per 10 seconds
throttle = HTTPThrottle(uid="api_v3", rate="1000/500ms")  # 1000 per 500ms

# Supported units: ms, s/sec/second, m/min/minute, h/hr/hour, d/day
throttle = HTTPThrottle(uid="daily", rate="10000/d")  # 10000 per day

# Rate object for complex configurations
rate = Rate(limit=100, minutes=5, seconds=30)  # 100 per 5.5 minutes
throttle = HTTPThrottle(uid="complex", rate=rate)
```

### Custom Client Identification

Customize how clients are identified for rate limiting:

```python
from starlette.requests import HTTPConnection
from traffik.throttles import HTTPThrottle

async def user_based_identifier(connection: HTTPConnection) -> str:
    """Identify by user ID from JWT token"""
    user_id = extract_user_id(connection.headers.get("authorization"))
    return f"user:{user_id}"

throttle = HTTPThrottle(
    uid="user_limit",
    rate="100/h",
    identifier=user_based_identifier,
)
```

### Exempting Connections

Skip throttling for specific clients by returning `UNLIMITED`:

```python
import typing
from starlette.requests import HTTPConnection
from traffik import UNLIMITED
from traffik.throttles import HTTPThrottle

async def admin_bypass_identifier(connection: HTTPConnection) -> typing.Any:
    """Bypass throttling for admin users"""
    user_role = extract_role(connection.headers.get("authorization"))
    
    if user_role == "admin":
        return UNLIMITED  # Admins bypass throttling
    
    # Regular users get normal identification
    user_id = extract_user_id(connection.headers.get("authorization"))
    return f"user:{user_id}"

throttle = HTTPThrottle(
    uid="api_limit",
    rate="50/m",
    identifier=admin_bypass_identifier,
)
```

### Custom Throttled Response

Customize the response when rate limits are exceeded:

```python
from starlette.requests import HTTPConnection
from starlette.exceptions import HTTPException

async def custom_throttled_handler(
    connection: HTTPConnection, 
    wait_ms: int,
    *args,
    **kwargs
):
    wait_seconds = wait_ms // 1000
    raise HTTPException(
        status_code=429,
        detail={
            "error": "rate_limit_exceeded",
            "message": f"Too many requests. Retry in {wait_seconds}s",
            "retry_after": wait_seconds
        },
        headers={"Retry-After": str(wait_seconds)},
    )

throttle = HTTPThrottle(
    uid="api",
    rate="100/m",
    handle_throttled=custom_throttled_handler,
)
```

## More on Usage

### Multiple Rate Limits per Endpoint

Apply both burst and sustained limits:

```python
from fastapi import FastAPI, Depends
from traffik.throttles import HTTPThrottle

# Burst limit: 10 per minute
burst_limit = HTTPThrottle(uid="burst", rate="10/m")

# Sustained limit: 100 per hour
sustained_limit = HTTPThrottle(uid="sustained", rate="100/h")

@app.get(
    "/api/data",
    dependencies=[Depends(burst_limit), Depends(sustained_limit)]
)
async def get_data():
    return {"data": "value"}
```

### Per-User Rate Limiting

```python
from traffik.throttles import HTTPThrottle
from starlette.requests import Request

async def user_identifier(request: Request) -> str:
    user_id = extract_user_from_token(request.headers.get("authorization"))
    return f"user:{user_id}"

user_throttle = HTTPThrottle(
    uid="user_quota",
    rate="1000/h",
    identifier=user_identifier,
)
```

### Strategy Selection Based on Use Case

```python
from traffik.throttles import HTTPThrottle
from traffik.strategies import (
    FixedWindowStrategy,
    SlidingWindowLogStrategy,
    TokenBucketStrategy,
    LeakyBucketStrategy,
)

# Public API - allow bursts
public_api = HTTPThrottle(
    uid="public",
    rate="100/m",
    strategy=TokenBucketStrategy(burst_size=150),
)

# Payment API - strict enforcement
payment_api = HTTPThrottle(
    uid="payments",
    rate="10/m",
    strategy=SlidingWindowLogStrategy(),
)

# Third-party API - smooth output
external_api = HTTPThrottle(
    uid="external",
    rate="50/m",
    strategy=LeakyBucketStrategy(),
)

# Simple rate limiting - default
simple_api = HTTPThrottle(
    uid="simple",
    rate="200/m",
    # Uses FixedWindowStrategy by default
)
```

### Dynamic Backend Resolution for Multi-Tenant Applications

Enable runtime backend switching for multi-tenant SaaS applications:

```python
from fastapi import FastAPI, Request, Depends
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
import jwt

# Shared throttle with dynamic backend resolution
api_throttle = HTTPThrottle(
    uid="api_quota",
    rate="1000/h",
    dynamic_backend=True,  # Resolve backend at runtime
)

TENANT_CONFIG = {
    "enterprise": {"redis_url": "redis://enterprise:6379/0", "multiplier": 5.0},
    "premium": {"redis_url": "redis://premium:6379/0", "multiplier": 2.0},
    "free": {"redis_url": None, "multiplier": 1.0},
}

async def tenant_middleware(request: Request, call_next):
    """Set up tenant-specific backend based on JWT"""
    token = request.headers.get("authorization", "").split(" ")[1] if request.headers.get("authorization") else None
    
    if token:
        payload = jwt.decode(token, "secret", algorithms=["HS256"])
        tier = payload.get("tenant_tier", "free")
        tenant_id = payload.get("tenant_id", "default")
    else:
        tier, tenant_id = "free", "anonymous"
    
    config = TENANT_CONFIG[tier]
    
    # Select backend based on tenant tier
    if config["redis_url"]:
        backend = RedisBackend(
            connection=config["redis_url"],
            namespace=f"tenant_{tenant_id}",
            persistent=True
        )
    else:
        backend = InMemoryBackend(namespace=f"tenant_{tenant_id}")
    
    # Execute within tenant's backend context
    async with backend:
        return await call_next(request)

app = FastAPI()
app.middleware("http")(tenant_middleware)

@app.get("/api/data")
async def get_data(request: Request, _: None = Depends(api_throttle)):
    return {"data": "tenant-specific data"}
```

**When to use dynamic backends:**

- ✅ Multi-tenant SaaS with per-tenant storage
- ✅ A/B testing different strategies
- ✅ Environment-specific backends
- ❌ Simple shared storage (use explicit `backend` parameter instead)

**Important:** Dynamic backend resolution adds slight overhead and complexity. Only use when you need runtime backend switching.

### Application Lifespan Management

Properly manage backend lifecycle:

#### Starlette

```python
from starlette.applications import Starlette
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="app")

app = Starlette(
    routes=[...],
    lifespan=backend.lifespan,  # Automatic cleanup
)
```

#### FastAPI

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from traffik.backends.redis import RedisBackend

backend = RedisBackend(connection="redis://localhost:6379/0", namespace="app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with backend(app):
        yield
    # Shutdown - backend cleanup automatic

app = FastAPI(lifespan=lifespan)
```

## Throttle Middleware

Apply rate limiting across multiple endpoints with sophisticated filtering and routing logic.

### Basic Middleware Setup

#### FastAPI

```python
from fastapi import FastAPI
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

app = FastAPI()
backend = InMemoryBackend(namespace="api")

# Create throttle
api_throttle = HTTPThrottle(uid="api_global", rate="100/m")

# Wrap in middleware throttle
middleware_throttle = MiddlewareThrottle(api_throttle)

# Add middleware
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[middleware_throttle],
    backend=backend
)
```

#### Starlette

```python
from starlette.applications import Starlette
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="api")

api_throttle = HTTPThrottle(uid="api_throttle", rate="50/m")
middleware_throttle = MiddlewareThrottle(api_throttle)

app = Starlette(
    routes=[...],
    middleware=[
        (ThrottleMiddleware, {
            "middleware_throttles": [middleware_throttle],
            "backend": backend
        })
    ]
)
```

### Advanced Filtering

#### Method-Based Filtering

```python
# Strict limits for write operations
write_throttle = HTTPThrottle(uid="writes", rate="10/m")
read_throttle = HTTPThrottle(uid="reads", rate="1000/m")

write_middleware = MiddlewareThrottle(
    write_throttle,
    methods={"POST", "PUT", "DELETE"}
)

read_middleware = MiddlewareThrottle(
    read_throttle,
    methods={"GET", "HEAD"}
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[write_middleware, read_middleware],
    backend=backend
)
```

#### Path Pattern Filtering

```python
# String patterns and regex
api_throttle = HTTPThrottle(uid="api", rate="100/m")
admin_throttle = HTTPThrottle(uid="admin", rate="5/m")

api_middleware = MiddlewareThrottle(
    api_throttle,
    path="/api/"  # Starts with /api/
)

admin_middleware = MiddlewareThrottle(
    admin_throttle,
    path=r"^/admin/.*"  # Regex pattern
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[admin_middleware, api_middleware],
    backend=backend
)
```

#### Custom Hook-Based Filtering

```python
from starlette.requests import HTTPConnection

async def authenticated_only(connection: HTTPConnection) -> bool:
    """Apply throttle only to authenticated users"""
    return connection.headers.get("authorization") is not None

async def intensive_operations(connection: HTTPConnection) -> bool:
    """Throttle resource-intensive endpoints"""
    intensive_paths = ["/api/reports/", "/api/analytics/", "/api/exports/"]
    return any(connection.scope["path"].startswith(p) for p in intensive_paths)

auth_throttle = HTTPThrottle(uid="auth", rate="200/m")
intensive_throttle = HTTPThrottle(uid="intensive", rate="20/m")

auth_middleware = MiddlewareThrottle(auth_throttle, hook=authenticated_only)
intensive_middleware = MiddlewareThrottle(intensive_throttle, hook=intensive_operations)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[intensive_middleware, auth_middleware],
    backend=backend
)
```

#### Combined Filtering

```python
async def authenticated_only(connection: HTTPConnection) -> bool:
    return connection.headers.get("authorization") is not None

complex_throttle = HTTPThrottle(uid="complex", rate="25/m")

# Combines ALL criteria: path AND method AND hook
complex_middleware = MiddlewareThrottle(
    complex_throttle,
    path="/api/",
    methods={"POST"},
    hook=authenticated_only
)
```

### Best Practices

1. **Order Matters**: Place more specific throttles before general ones
2. **Early Placement**: Add throttle middleware early in the stack to reject requests before expensive processing
3. **Production Backends**: Use Redis for multi-instance deployments
4. **Monitoring**: Log throttle hits for capacity planning
5. **Graceful Responses**: Provide clear error messages with retry information

```python
# Example: Optimal middleware ordering
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        admin_middleware,      # Most specific
        intensive_middleware,  # Specific paths
        api_middleware,        # General API
    ],
    backend=redis_backend
)
```

## Error Handling

Traffik provides specific exceptions for different scenarios:

### Exception Types

```python
from traffik.exceptions import (
    TraffikException,           # Base exception
    ConfigurationError,         # Invalid configuration
    AnonymousConnection,        # Cannot identify client
    ConnectionThrottled,        # Rate limit exceeded (HTTP 429)
    BackendError,               # Backend operation failed
)
```

### Handling Rate Limit Exceptions

```python
from fastapi import FastAPI, Request
from traffik.exceptions import ConnectionThrottled
from starlette.responses import JSONResponse

@app.exception_handler(ConnectionThrottled)
async def throttle_handler(request: Request, exc: ConnectionThrottled):
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "retry_after_seconds": exc.retry_after,
            "message": exc.detail
        },
        headers={"Retry-After": str(exc.retry_after)}
    )
```

### Handling Configuration Errors

```python
from traffik.exceptions import ConfigurationError, BackendConnectionError

try:
    backend = RedisBackend(connection="invalid://url")
    await backend.initialize()
except BackendConnectionError as e:
    logger.error(f"Failed to connect to Redis: {e}")
except ConfigurationError as e:
    logger.error(f"Invalid configuration: {e}")
```

### Handling Anonymous Connections

```python
from traffik.exceptions import AnonymousConnection
from traffik.backends.base import connection_identifier

async def safe_identifier(connection: HTTPConnection) -> str:
    try:
        return await connection_identifier(connection)
    except AnonymousConnection:
        # Fall back to a default identifier
        return f"anonymous:{connection.scope['path']}"
```

## Testing

Comprehensive testing example using `InMemoryBackend`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

@pytest.fixture
async def backend():
    backend = InMemoryBackend(namespace="test", persistent=False)
    async with backend:
        yield backend

@pytest.mark.anyio
async def test_throttling(backend):
    throttle = HTTPThrottle(uid="test", rate="2/s")

    async def endpoint(request):
        await throttle(request)
        return JSONResponse({"status": "ok"})
    
    app = Starlette(
        routes=[Route("/test", endpoint, methods=["GET"])],
        lifespan=backend.lifespan,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # First 2 requests succeed
        r1 = await client.get("/test")
        r2 = await client.get("/test")
        assert r1.status_code == 200
        assert r2.status_code == 200
        
        # Third request throttled
        r3 = await client.get("/test")
        assert r3.status_code == 429
```

### Testing Different Strategies

```python
from traffik.strategies import (
    FixedWindowStrategy,
    SlidingWindowLogStrategy,
    TokenBucketStrategy,
)

@pytest.mark.parametrize("strategy", [
    FixedWindowStrategy(),
    SlidingWindowLogStrategy(),
    TokenBucketStrategy(),
])
async def test_strategy(backend, strategy):
    throttle = HTTPThrottle(uid="test", rate="5/s", strategy=strategy)
    # Test throttle behavior with different strategies
```

## API Reference

### Throttle Classes

#### `HTTPThrottle`

HTTP request rate limiting with flexible configuration.

```python
HTTPThrottle(
    uid: str,                          # Unique identifier
    rate: Union[Rate, str],           # "100/m" or Rate object
    identifier: Optional[Callable],    # Client ID function
    handle_throttled: Optional[Callable],  # Custom handler
    strategy: Optional[Strategy],      # Rate limiting strategy
    backend: Optional[ThrottleBackend],  # Storage backend
    dynamic_backend: bool = False,     # Runtime backend resolution
    min_wait_period: Optional[int] = None,  # Minimum wait (ms)
    headers: Optional[Dict[str, str]] = None,  # Extra headers
)
```

#### `WebSocketThrottle`

WebSocket message rate limiting.

```python
WebSocketThrottle(
    uid: str,
    rate: Union[Rate, str],
    # ... same parameters as HTTPThrottle
)
```

#### `BaseThrottle`

Base class for custom throttle implementations. Override `get_key()` for custom key generation.

### Strategy Classes

All strategies are dataclasses with frozen=True for immutability.

#### `FixedWindowStrategy()` (Default)

Simple fixed-window counting. Fast and memory-efficient.

#### `SlidingWindowLogStrategy()`

Most accurate, maintains request log. Memory: O(limit).

#### `SlidingWindowCounterStrategy()`

Sliding window with counters. Balance between accuracy and memory.

#### `TokenBucketStrategy(burst_size: Optional[int] = None)`

Allows controlled bursts. `burst_size` defaults to rate limit.

#### `TokenBucketWithDebtStrategy(max_debt: Optional[int] = None)`

Token bucket with debt tracking for smoother recovery.

#### `LeakyBucketStrategy()`

Perfectly smooth traffic output. No bursts allowed.

#### `LeakyBucketWithQueueStrategy(queue_size: Optional[int] = None)`

Leaky bucket with request queuing.

### Backend Classes

#### `InMemoryBackend`

```python
InMemoryBackend(
    namespace: str = "inmemory",
    persistent: bool = False,
)
```

#### `RedisBackend`

```python
RedisBackend(
    connection: Union[str, Redis],
    namespace: str,
    persistent: bool = True,
)
```

#### `MemcachedBackend`

```python
MemcachedBackend(
    host: str = "localhost",
    port: int = 11211,
    namespace: str = "memcached",
    pool_size: int = 2,
    persistent: bool = False,
)
```

### Rate Class

```python
Rate(
    limit: int,
    milliseconds: int = 0,
    seconds: int = 0,
    minutes: int = 0,
    hours: int = 0,
)

# Or use string format
Rate.parse("100/m")  # 100 per minute
Rate.parse("5/10s")  # 5 per 10 seconds
```

### Middleware Classes

#### `MiddlewareThrottle`

```python
MiddlewareThrottle(
    throttle: BaseThrottle,
    path: Optional[Union[str, Pattern]] = None,
    methods: Optional[Set[str]] = None,
    hook: Optional[Callable] = None,
)
```

#### `ThrottleMiddleware`

```python
ThrottleMiddleware(
    app: ASGIApp,
    middleware_throttles: List[MiddlewareThrottle],
    backend: Optional[ThrottleBackend] = None,
)
```

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Setup

```bash
git clone https://github.com/ti-oluwa/traffik.git
cd traffik
uv sync --extra dev

# Run tests
uv run pytest

# Run linting
uv run ruff check src/ tests/

# Run formatting  
uv run ruff format src/ tests/
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history and changes.
