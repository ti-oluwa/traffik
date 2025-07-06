# Traffik - A Starlette throttling library

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
<!-- [![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/) -->

Traffik provides rate limiting capabilities for starlette-based applications like FastAPI with support for both HTTP and WebSocket connections. It uses a **token bucket algorithm** for smooth, burst-friendly rate limiting and offers multiple backend options including in-memory storage for development and Redis for production environments.

The library features a dynamic backend system for multi-tenant applications, customizable throttling strategies, and comprehensive error handling. Whether you need simple per-endpoint limits or complex multi-tenant rate limiting, Traffik provides the flexibility to handle your use case.

Traffik was inspired by [fastapi-limiter](https://github.com/long2ice/fastapi-limiter), and some of the code is adapted from it. However, Traffik aims to provide a more flexible and extensible solution with a focus on ease of use and advanced features like dynamic backend resolution.

## Features

- 🚀 **Easy Integration**: Simple decorator and dependency-based throttling
- 🪣 **Token Bucket Algorithm**: Smooth, burst-friendly rate limiting with gradual token refill
- 🔄 **Multiple Backends**: In-memory (development) and Redis (production) support
- 🌐 **Protocol Support**: Both HTTP and WebSocket throttling
- 🏢 **Dynamic Backend Resolution**: Multi-tenant support with runtime backend switching
- 🔧 **Flexible Configuration**: Time-based limits with multiple time units
- 🎯 **Per-Route Throttling**: Individual limits for different endpoints
- 📊 **Client Identification**: Customizable client identification strategies
- 🛡️ **Thread-Safe Design**: Immutable throttles with proper async locking
- ⚡ **High Performance**: Optimized scripts and efficient in-memory operations

## How Token Bucket Algorithm Works

Traffik uses a **token bucket algorithm** for rate limiting, which provides several advantages over traditional fixed-window approaches:

### Token Bucket Concept

Think of a bucket that holds tokens:

- **Bucket Capacity**: Your rate limit (e.g., 100 requests)
- **Token Refill Rate**: Tokens are added continuously over time
- **Request Processing**: Each request consumes one token
- **Burst Handling**: Allows temporary bursts up to bucket capacity

### Example: 100 requests per hour

```python
# Configuration
limit = 100        # Bucket holds 100 tokens max
expires_after = 3600000  # 1 hour in milliseconds
refill_rate = 100 / 3600000  # ≈ 0.0278 tokens per millisecond

# Behavior:
# - Bucket starts full (100 tokens)
# - Client can make 100 requests immediately (burst)
# - After burst, tokens refill at ~1.67 per minute
# - Sustained rate: ~27.8 requests per 1000 seconds
```

### Advantages

1. **Smooth Rate Limiting**: No sudden resets at window boundaries
2. **Burst Tolerance**: Allows legitimate traffic spikes
3. **Fairness**: Gradual token replenishment prevents starvation
4. **Predictable**: Wait times are calculated precisely

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

Let's also install `fastapi` if you haven't already:

```bash
uv add traffik[fastapi]

# or using pip
pip install traffik[fastapi]
```

### With Redis Support

```bash
uv add traffik[redis]

# or using pip
pip install traffik[redis]
```

### With Support for all Features

```bash
uv add traffik[all]

# or using pip
pip install traffik[all]
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

### 1. Basic HTTP Throttling

### For Starlette

````python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

# Create backend
throttle_backend = InMemoryBackend(prefix="myapp", persistent=False)

throttle = HTTPThrottle(
    limit=5,        # 5 requests
    seconds=10,     # per 10 seconds
)

async def throttled_endpoint(request: Request):
    """
    Endpoint that is throttled.
    """
    await throttle(request)
    return JSONResponse({"message": "Success"})


app = Starlette(
    routes=[
        Route("/throttled", throttled_endpoint, methods=["GET"]),
    ],
    lifespan=throttle_backend.lifespan,  # Use `throttle_backend.lifespan` for cleanup
)
````

#### For FastAPI

```python
from fastapi import FastAPI, Depends
from contextlib import asynccontextmanager
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

# Create backend
throttle_backend = InMemoryBackend()

# Create FastAPI app lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with throttle_backend(app):
        yield

app = FastAPI(lifespan=lifespan)

# Create throttle
throttle = HTTPThrottle(
    limit=10,        # 10 requests
    seconds=60,      # per 60 seconds
)

@app.get("/api/hello", dependencies=[Depends(throttle)])
async def say_hello():
    return {"message": "Hello World"}

```

### 2. Using Decorators

Currently, the available decorator is only for FastAPI applications.

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager
from traffik.throttles import HTTPThrottle
from traffik.decorators import throttled # Requires `traffik[fastapi]` or `traffik[all]`
from traffik.backends.redis import RedisBackend

throttle_backend = RedisBackend(
    connection="redis://localhost:6379/0",
    prefix="myapp",  # Key prefix
    persistent=True,  # Survive restarts
)

# Setup FastAPI app with lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with throttle_backend(app):
        yield

app = FastAPI(lifespan=lifespan)


@app.get("/api/limited")
@throttled(HTTPThrottle(limit=5, minutes=1))
async def limited_endpoint():
    return {"data": "Limited access"}

```

### 3. WebSocket Throttling

WebSocket throttling limits the rate of messages a client can send over a WebSocket connection:

```python
from traffik.throttles import WebSocketThrottle
from starlette.websockets import WebSocket
from starlette.exceptions import HTTPException

ws_throttle = WebSocketThrottle(limit=3, seconds=10)

async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    
    while True:
        try:
            data = await websocket.receive_json()
            await ws_throttle(websocket)  # Check rate limit
            
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
        except Exception:
            await websocket.send_json({
                "status": "error",
                "detail": "Internal error"
            })
            break
    
    await websocket.close()
```

Use this WebSocket endpoint in your application:

```python
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from fastapi import FastAPI

# For Starlette
app = Starlette(routes=[WebSocketRoute("/ws/limited", ws_endpoint)])

# For FastAPI  
app = FastAPI()
app.websocket("/ws/limited")(ws_endpoint)
```

## Backends

### In-Memory Backend

Perfect for development, testing, and single-process applications:

```python
from traffik.backends.inmemory import InMemoryBackend

inmemory_throttle_backend = InMemoryBackend(
    prefix="myapp",           # Key prefix
    persistent=False,         # Don't persist across restarts
)
```

**Pros:**

- No external dependencies
- Fast and simple
- Great for testing

**Cons:**

- Not suitable for multi-process/distributed systems
- Data is lost on restart (even with persistent=True)

### Redis Backend

Recommended for production environments:

```python
from traffik.backends.redis import RedisBackend

# From URL
redis_throttle_backend = RedisBackend(
    connection="redis://localhost:6379/0",
    prefix="myapp",
    persistent=True,  # Survive restarts
)

# From Redis instance
import redis.asyncio as redis
redis_client = redis.Redis(host='localhost', port=6379, db=0)
redis_throttle_backend = RedisBackend(
    connection=redis_client,
    prefix="myapp",
)
```

**Pros:**

- Distributed throttling across multiple processes
- Persistence across restarts
- Production-ready

**Cons:**

- Requires Redis server
- Additional infrastructure dependency

### Custom Backends

You can create custom backends by subclassing `ThrottleBackend` and implementing the token bucket algorithm. Here's a simplified example:

```python
import time
import typing
from traffik.backends.base import ThrottleBackend
from traffik.types import HTTPConnectionT

class CustomBackend(ThrottleBackend[typing.Dict, HTTPConnectionT]):
    """Custom backend implementing token bucket algorithm"""
    
    def __init__(self, storage_config: str = "custom://config", **kwargs):
        # Initialize your storage connection
        self._storage = {}
        super().__init__(connection=self._storage, **kwargs)
    
    async def get_wait_period(self, key: str, limit: int, expires_after: int) -> int:
        """Token bucket implementation for custom storage"""
        now = int(time.monotonic() * 1000)
        
        # Get or create token bucket record
        record = self._storage.get(key, {
            "tokens": float(limit),
            "last_refill": now
        })
        
        # Calculate tokens to add based on elapsed time
        time_elapsed = now - record["last_refill"]
        refill_rate = limit / expires_after  # tokens per millisecond
        tokens_to_add = time_elapsed * refill_rate
        
        # Refill tokens (capped at limit)
        record["tokens"] = min(float(limit), record["tokens"] + tokens_to_add)
        record["last_refill"] = now
        
        # Check if request can be served
        if record["tokens"] >= 1.0:
            record["tokens"] -= 1.0
            self._storage[key] = record
            return 0  # Allow request
        
        # Calculate wait time for next token
        tokens_needed = 1.0 - record["tokens"]
        wait_time = int(tokens_needed / refill_rate)
        self._storage[key] = record
        return wait_time
    
    async def reset(self) -> None:
        """Reset all throttle records"""
        pattern = str(self.key_pattern)
        keys_to_delete = [k for k in self._storage.keys() if k.startswith(pattern.replace("*", ""))]
        for key in keys_to_delete:
            del self._storage[key]
    
    async def close(self) -> None:
        """Clean up resources"""
        self._storage.clear()

# Usage
custom_backend = CustomBackend(prefix="myapp")
throttle = HTTPThrottle(limit=100, minutes=1, backend=custom_backend)
```

## Configuration Options

### Time Units

Throttles support multiple time units that can be combined:

```python
HTTPThrottle(
    limit=100,
    milliseconds=500,  # 500ms
    seconds=30,        # + 30 seconds  
    minutes=5,         # + 5 minutes
    hours=1,           # + 1 hour
    # Total: 1 hour, 5 minutes, 30.5 seconds
)
```

### Custom Client Identification

By default, clients are identified by IP address and path. You can customize this:

```python
from starlette.requests import HTTPConnection
from traffik.throttles import HTTPThrottle

async def custom_identifier(connection: HTTPConnection):
    # Use user ID from JWT token
    user_id = extract_user_id(connection.headers.get("authorization"))
    return f"user:{user_id}:{connection.scope['path']}"

throttle = HTTPThrottle(
    limit=10,
    minutes=1,
    identifier=custom_identifier, # Override default (backend) identifier
)
```

### Exempting connections from Throttling

You can exclude certain connections from throttling by writing a custom identifier that returns `traffik.UNLIMITED` for those connections. This is useful when you have throttles you want to skip for specific clients and/or routes.

```python
import typing
from starlette.requests import HTTPConnection
from traffik import UNLIMITED, HTTPThrottle

def extract_user_id(authorization: str) -> str:
    # Dummy function to extract user ID from JWT token
    # Replace with actual JWT decoding logic
    return authorization.split(" ")[1] if authorization else "anonymous"

def extract_user_role(authorization: str) -> str:
    # Dummy function to extract user role from JWT token
    # Replace with actual JWT decoding logic
    return "admin" if "admin" in authorization else "user"

async def user_identifier(connection: HTTPConnection) -> str:
    # Use user ID from JWT token
    user_id = extract_user_id(connection.headers.get("authorization"))
    return f"user:{user_id}:{connection.scope['path']}"

async def no_throttle_admin_identifier(connection: HTTPConnection) -> typing.Any:
    user_role = extract_user_role(connection.headers.get("authorization"))
    if user_role == "admin":
        return UNLIMITED  # Skip throttling for admin users
    return user_identifier(connection)

throttle = HTTPThrottle(
    limit=10,
    minutes=1,
    identifier=no_throttle_admin_identifier,  # Override default (backend) identifier
)
```

### Custom Throttled Response

Customize what happens when a client is throttled:

```python
from starlette.requests import HTTPConnection
from starlette.exceptions import HTTPException
import traffik


async def custom_throttled_handler(
    connection: HTTPConnection, 
    wait_period: int, 
    *args, **kwargs
):
    raise HTTPException(
        status_code=429,
        detail=f"Too many requests. Try again in {wait_period // 1000} seconds.",
        headers={"Retry-After": str(wait_period // 1000)},
    )

throttle = traffik.HTTPThrottle(
    limit=5,
    minutes=1,
    handle_throttled=custom_throttled_handler,
)
```

## Advanced Usage

### Multiple Rate Limits

Different limits can be applied to the same endpoint using multiple throttles. This is useful for burst and sustained limits. Take the FastAPI example below:

```python
from fastapi import FastAPI, Depends
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

throttle_backend = InMemoryBackend(prefix="myapp", persistent=False)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with throttle_backend(app):
        yield

app = FastAPI(lifespan=lifespan)

# Burst limit: 10 requests per minute
burst_throttle = HTTPThrottle(limit=10, minutes=1)

# Sustained limit: 100 requests per hour  
sustained_throttle = HTTPThrottle(limit=100, hours=1)

async def search_db(query: str):
    # Simulate a database search
    return {"query": query, "result": ...}


@app.get(
    "/api/data",
    dependencies=[
        Depends(burst_throttle), 
        Depends(sustained_throttle)
    ],
)
async def search(query: str):
    data = await search_db(query)
    return {"message": "Data retrieved", "data": data}
```

### Per-User Rate Limiting

```python
from traffik.throttles import HTTPThrottle
from starlette.requests import Request


async def get_user_id(request: Request):
    # Extract user ID from request state
    return request.state.user.id if hasattr(request.state, 'user') else None


async def user_identifier(request: Request) -> str:
    # Extract user ID from JWT or session
    user_id = await get_user_id(request)
    return f"user:{user_id}"


user_throttle = HTTPThrottle(
    limit=100,
    hours=1,
    identifier=user_identifier,
)
```

### Dynamic Backend Resolution for Multi-Tenant Applications

The `dynamic_backend=True` feature enables runtime backend switching, perfect for multi-tenant SaaS applications where different tenants require isolated rate limiting storage.

#### Real-World Multi-Tenant Example

```python
from fastapi import FastAPI, Request, Depends, HTTPException
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
import jwt

# Shared throttle instance for all tenants
api_quota_throttle = HTTPThrottle(
    uid="api_quota",
    limit=1000,  # 1000 requests
    hours=1,     # per hour
    dynamic_backend=True  # Enable runtime backend resolution
)

# Tenant configuration
TENANT_CONFIG = {
    "enterprise": {
        "redis_url": "redis://enterprise-cluster:6379/0",
        "quota_multiplier": 5.0,  # 5x higher limits
    },
    "premium": {
        "redis_url": "redis://premium-redis:6379/0", 
        "quota_multiplier": 2.0,  # 2x higher limits
    },
    "free": {
        "redis_url": None,  # Use in-memory for free tier
        "quota_multiplier": 1.0,
    }
}

def extract_tenant_from_jwt(authorization: str) -> dict:
    """Extract tenant info from JWT token"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid authorization")
    
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, "your-secret", algorithms=["HS256"])
        tenant_tier = payload.get("tenant_tier", "free")
        tenant_id = payload.get("tenant_id", "unknown")
        return {"tier": tenant_tier, "id": tenant_id}
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

async def tenant_middleware(request: Request, call_next):
    """Middleware to set up tenant-specific backend context"""
    # Extract tenant from request
    auth_header = request.headers.get("authorization", "")
    tenant_info = extract_tenant_from_jwt(auth_header)
    
    # Get tenant configuration
    tenant_config = TENANT_CONFIG.get(tenant_info["tier"], TENANT_CONFIG["free"])
    
    # Create tenant-specific backend
    if tenant_config["redis_url"]:
        # Premium/Enterprise: Dedicated Redis instance
        backend = RedisBackend(
            connection=tenant_config["redis_url"],
            prefix=f"tenant_{tenant_info['id']}",
            persistent=True
        )
    else:
        # Free tier: In-memory backend
        backend = InMemoryBackend(
            prefix=f"tenant_{tenant_info['id']}",
            persistent=False
        )
    
    # Set tenant context for request
    request.state.tenant = tenant_info
    
    # Execute request within backend context
    async with backend:
        response = await call_next(request)
        return response

app = FastAPI()
app.middleware("http")(tenant_middleware)

@app.get("/api/data")
async def get_data(request: Request, _: None = Depends(api_quota_throttle)):
    """API endpoint with tenant-aware rate limiting"""
    tenant = request.state.tenant
    return {
        "message": f"Data for {tenant['tier']} tenant {tenant['id']}",
        "remaining_quota": "Calculated based on tenant tier"
    }

# Usage example:
# curl -H "Authorization: Bearer <jwt-with-tenant-info>" http://localhost:8000/api/data
```

#### Multi-Environment Testing Example

```python
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend

# Shared throttle for testing different backends
test_throttle = HTTPThrottle(
    uid="test_throttle",
    limit=5,
    seconds=10,
    dynamic_backend=True
)

async def test_backend_switching():
    """Test the same throttle with different backends"""
    
    # Test with Redis backend
    redis_backend = RedisBackend("redis://localhost:6379/1", prefix="test_redis")
    async with redis_backend:
        for i in range(3):
            await test_throttle(mock_request)
            print(f"Redis backend - Request {i+1} successful")
    
    # Test with in-memory backend (completely separate state)
    inmemory_backend = InMemoryBackend(prefix="test_memory")
    async with inmemory_backend:
        for i in range(3):
            await test_throttle(mock_request)  # Fresh counter
            print(f"In-memory backend - Request {i+1} successful")
            
    # Nested contexts for A/B testing
    backend_a = InMemoryBackend(prefix="variant_a")
    backend_b = InMemoryBackend(prefix="variant_b")
    
    async with backend_a:
        await test_throttle(mock_request)  # Uses backend_a
        
        async with backend_b:
            await test_throttle(mock_request)  # Switches to backend_b
            await test_throttle(mock_request)  # Still backend_b
            
        await test_throttle(mock_request)  # Back to backend_a
```

#### Important Considerations

**When to Use Dynamic Backends:**

- ✅ Multi-tenant SaaS with tenant-specific storage requirements
- ✅ A/B testing different rate limiting strategies
- ✅ Environment-specific backend selection (dev/staging/prod)
- ✅ Request-type based storage (e.g., different limits for API vs Web requests)

**When NOT to Use:**

- ❌ Simple shared storage across services (use explicit `backend` parameter)
- ❌ Single-tenant applications (adds unnecessary complexity)
- ❌ When backend choice is known at application startup

**Performance Impact:**

- Small overhead: Backend resolution on each request
- Memory efficiency: Only one throttle instance needed per limit type
- Context switching: May cause data fragmentation if inconsistent

### Application Lifespan Management

For Starlette applications, you can manage the backend lifecycle using the `lifespan` context manager on the throttle backend. This ensures that the backend is properly initialized and cleaned up when the application starts and stops.

```python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle

# Create backend
throttle_backend = InMemoryBackend(prefix="myapp", persistent=False)
throttle = HTTPThrottle(
    limit=5,        # 5 requests
    seconds=10,     # per 10 seconds
)
async def throttled_endpoint(request: Request):
    """
    Endpoint that is throttled.
    """
    await throttle(request)
    return JSONResponse({"message": "Success"})

app = Starlette(
    routes=[
        Route("/throttled", throttled_endpoint, methods=["GET"]),
    ],
    lifespan=throttle_backend.lifespan,  # Use `throttle_backend.lifespan` for cleanup
)
```

For FastAPI applications, you can use the `lifespan` attribute of the throttle backend to manage the backend lifecycle. However, you can also use the `asynccontextmanager` decorator to create a lifespan context manager for your FastAPI application. This allows you to perform other setup and teardown tasks when the application starts and stops.

```python
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Other setup tasks can go here
    async with backend(app):
        yield
    # Shutdown - backend cleanup handled automatically

app = FastAPI(lifespan=lifespan)
```

## Throttle Middleware

Traffik provides powerful middleware capabilities that allow you to apply rate limiting across multiple endpoints with sophisticated filtering and routing logic. The middleware system is ideal for applying consistent rate limiting policies across your application while maintaining flexibility for specific requirements.

### Basic Middleware Setup

#### For FastAPI Applications

```python
from fastapi import FastAPI
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

app = FastAPI()
backend = InMemoryBackend(prefix="api")

# Create a throttle instance
api_throttle = HTTPThrottle(
    uid="api_global",
    limit=100,
    minutes=1
)

# Wrap it in middleware throttle - applies to all endpoints
basic_middleware_throttle = MiddlewareThrottle(api_throttle)

# Add middleware
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[basic_middleware_throttle],
    backend=backend
)

@app.get("/api/users")
async def get_users():
    return {"users": []}

@app.get("/api/posts") 
async def get_posts():
    return {"posts": []}
```

#### For Starlette Applications

```python
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle
from traffik.throttles import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

async def users_endpoint(request):
    return JSONResponse({"users": []})

async def posts_endpoint(request): 
    return JSONResponse({"posts": []})

backend = InMemoryBackend(prefix="api")

# Create throttle instance
api_throttle = HTTPThrottle(
    uid="api_throttle",
    limit=50,
    minutes=1
)

# Wrap in middleware throttle
middleware_throttle = MiddlewareThrottle(api_throttle)

app = Starlette(
    routes=[
        Route("/api/users", users_endpoint),
        Route("/api/posts", posts_endpoint),
    ],
    middleware=[
        (ThrottleMiddleware, {
            "middleware_throttles": [middleware_throttle],
            "backend": backend
        })
    ]
)
```

### Advanced Filtering Options

#### Method-Based Filtering

Apply different limits to different HTTP methods:

```python
# Strict limits for write operations
write_throttle = HTTPThrottle(
    uid="write_operations",
    limit=10,
    minutes=1
)

# Generous limits for read operations  
read_throttle = HTTPThrottle(
    uid="read_operations", 
    limit=1000,
    minutes=1
)

# Create middleware throttles with method filtering
write_middleware = MiddlewareThrottle(
    write_throttle,
    methods={"POST", "PUT", "DELETE"}  # Only write methods
)

read_middleware = MiddlewareThrottle(
    read_throttle,
    methods={"GET", "HEAD"}  # Only read methods
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[write_middleware, read_middleware],
    backend=backend
)
```

#### Path Pattern Filtering

Use string patterns or regex to target specific endpoints:

```python
# Create throttle instances
api_throttle = HTTPThrottle(uid="api_endpoints", limit=100, minutes=1)
admin_throttle = HTTPThrottle(uid="admin_endpoints", limit=5, minutes=1)
static_throttle = HTTPThrottle(uid="static_files", limit=10000, minutes=1)

# Create middleware throttles with path filtering
api_middleware = MiddlewareThrottle(
    api_throttle,
    path="/api/"  # Matches paths starting with /api/
)

admin_middleware = MiddlewareThrottle(
    admin_throttle,
    path="/admin/"  # Matches paths starting with /admin/
)

# For complex patterns, use regex strings
static_middleware = MiddlewareThrottle(
    static_throttle,
    path=r"^/(static|assets|media)/.*"  # Regex pattern for static files
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[admin_middleware, api_middleware, static_middleware],
    backend=backend
)
```

#### Custom Hook-Based Filtering

Implement complex business logic with custom hooks:

```python
from starlette.requests import HTTPConnection

# Create throttle instances  
auth_throttle = HTTPThrottle(uid="authenticated_users", limit=200, minutes=1)
intensive_throttle = HTTPThrottle(uid="intensive_operations", limit=20, minutes=1)
external_throttle = HTTPThrottle(uid="external_api_calls", limit=50, minutes=5)

async def authenticated_users_only(connection: HTTPConnection) -> bool:
    """Only apply throttle to authenticated users"""
    auth_header = connection.headers.get("authorization")
    return auth_header is not None and auth_header.startswith("Bearer ")

async def high_priority_endpoints(connection: HTTPConnection) -> bool:
    """Apply strict limits to resource-intensive endpoints"""
    intensive_paths = ["/api/reports/", "/api/analytics/", "/api/exports/", "/api/search"]
    path = connection.scope["path"]
    return any(path.startswith(intensive_path) for intensive_path in intensive_paths)

async def external_api_calls(connection: HTTPConnection) -> bool:
    """Identify requests that trigger external API calls"""
    headers = dict(connection.headers)
    return "x-external-api" in headers

# Create middleware throttles with custom hooks
auth_middleware = MiddlewareThrottle(
    auth_throttle,
    hook=authenticated_users_only
)

intensive_middleware = MiddlewareThrottle(
    intensive_throttle,
    hook=high_priority_endpoints
)

external_middleware = MiddlewareThrottle(
    external_throttle,
    hook=external_api_calls
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[intensive_middleware, external_middleware, auth_middleware],
    backend=backend
)
```

### Combined Filtering Logic

Combine multiple filters for precise targeting:

```python
# Create throttle instance
complex_throttle = HTTPThrottle(
    uid="authenticated_api_posts",
    limit=25,
    minutes=1
)

async def authenticated_users_only(connection: HTTPConnection) -> bool:
    auth_header = connection.headers.get("authorization")
    return auth_header is not None and auth_header.startswith("Bearer ")

# Complex middleware: POST requests to API endpoints by authenticated users
complex_middleware = MiddlewareThrottle(
    complex_throttle,
    path="/api/",                        # Path starts with /api/
    methods={"POST"},                    # Only POST requests
    hook=authenticated_users_only        # Only authenticated users
)

# This throttle will only apply to requests that match ALL criteria:
# - Path starts with /api/
# - Method is POST 
# - Hook function returns True (user is authenticated)
```

### Multi-Tenant Middleware

Use middleware with dynamic backends for multi-tenant applications:

```python
from fastapi import FastAPI, Request
from traffik.middleware import ThrottleMiddleware, MiddlewareThrottle
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
import jwt

# Create tenant-aware throttles
api_throttle = HTTPThrottle(
    uid="api_quota",
    limit=1000,
    hours=1,
    dynamic_backend=True  # Enable dynamic backend resolution
)

admin_throttle = HTTPThrottle(
    uid="admin_quota", 
    limit=100,
    hours=1,
    dynamic_backend=True
)

# Create middleware throttles with path filtering
api_middleware = MiddlewareThrottle(api_throttle, path="/api/")
admin_middleware = MiddlewareThrottle(admin_throttle, path="/admin/")

async def tenant_context_middleware(request: Request, call_next):
    """Set up tenant-specific backend context"""
    # Extract tenant from JWT (simplified)
    auth_header = request.headers.get("authorization", "")
    tenant_id = "default"
    
    if auth_header.startswith("Bearer "):
        try:
            token = auth_header.split(" ")[1]
            payload = jwt.decode(token, "secret", algorithms=["HS256"])
            tenant_id = payload.get("tenant_id", "default")
        except jwt.InvalidTokenError:
            pass
    
    # Choose backend based on tenant
    if tenant_id.startswith("enterprise_"):
        backend = RedisBackend(
            connection="redis://enterprise-redis:6379/0",
            prefix=f"tenant_{tenant_id}"
        )
    else:
        backend = InMemoryBackend(prefix=f"tenant_{tenant_id}")
    
    # Execute request within tenant's backend context
    async with backend:
        response = await call_next(request)
        return response

app = FastAPI()

# Add tenant middleware first
app.middleware("http")(tenant_context_middleware)

# Add throttle middleware
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[admin_middleware, api_middleware],
    # No backend specified - uses dynamic backend from context
)
```

### WebSocket Handling

The middleware automatically handles WebSocket connections properly:

```python
from fastapi import FastAPI, WebSocket

# Create throttle for WebSocket connections
ws_throttle = WebSocketThrottle(
    uid="websocket_connections",
    limit=10,
    minutes=1
)

# Create middleware throttle
ws_middleware = MiddlewareThrottle(ws_throttle)

app = FastAPI()
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[ws_middleware],
    backend=backend
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # WebSocket throttling is handled by middleware
    await websocket.send_text("Hello WebSocket!")
    await websocket.close()

# Regular HTTP endpoints also covered
@app.get("/api/data")
async def get_data():
    return {"data": "value"}
```

### Error Handling and Exemptions

#### Custom Exemption Logic

Create sophisticated exemption rules:

```python
async def admin_exemption_hook(connection: HTTPConnection) -> bool:
    """Exempt admin users from rate limiting"""
    auth_header = connection.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return True  # Apply throttle to non-authenticated users
    
    try:
        token = auth_header.split(" ")[1]
        payload = jwt.decode(token, "secret", algorithms=["HS256"])
        user_role = payload.get("role", "user")
        return user_role != "admin"  # False = exempt admin, True = throttle others
    except jwt.InvalidTokenError:
        return True  # Apply throttle to invalid tokens

# Create throttle for non-admin users
user_throttle = HTTPThrottle(uid="non_admin_users", limit=100, minutes=1)

# Throttle that exempts admin users
user_middleware = MiddlewareThrottle(
    user_throttle,
    hook=admin_exemption_hook
)
```

#### Throttled Response Customization

The middleware uses the same exception handling as individual throttles:

```python
from traffik.exceptions import ConnectionThrottled
from starlette.responses import JSONResponse

# Custom exception handler for middleware throttling
@app.exception_handler(ConnectionThrottled)
async def throttled_handler(request: Request, exc: ConnectionThrottled):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "message": f"Too many requests. Try again in {exc.retry_after} seconds.",
            "retry_after": exc.retry_after,
            "limit_type": "middleware_throttle"
        },
        headers={"Retry-After": str(int(exc.retry_after))}
    )
```

### Performance Considerations

#### Middleware Order

Place throttle middleware early in the stack for optimal performance:

```python
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

app = FastAPI()

# Create throttle
api_throttle = HTTPThrottle(uid="api_rate_limit", limit=1000, minutes=1)
api_middleware = MiddlewareThrottle(api_throttle)

# Add throttling early to reject requests before heavy processing
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[api_middleware],
    backend=backend
)

# Add other middleware after throttling
app.add_middleware(GZipMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"])
```

#### Redis Backend for Production

Use Redis backend for production middleware deployments:

```python
from traffik.backends.redis import RedisBackend

# Production Redis setup
production_backend = RedisBackend(
    connection="redis://redis-cluster:6379/0",
    prefix="prod_api",
    persistent=True
)

# Create production throttles
production_api_throttle = HTTPThrottle(
    uid="production_api",
    limit=10000,
    hours=1
)

expensive_operations_throttle = HTTPThrottle(
    uid="expensive_operations",
    limit=100,
    minutes=1
)

# Create middleware throttles with filtering
api_middleware = MiddlewareThrottle(production_api_throttle, path="/api/")
expensive_middleware = MiddlewareThrottle(
    expensive_operations_throttle,
    path=r"^/api/(search|analytics)/.*"  # Expensive endpoints
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[expensive_middleware, api_middleware],  # Order matters!
    backend=production_backend
)
```

### Best Practices

1. **Specific Before General**: Place more specific throttles before general ones in the middleware_throttles list
2. **Early Placement**: Add throttle middleware early in the middleware stack
3. **Production Backends**: Use Redis for multi-instance deployments
4. **Monitoring**: Log throttle hits for monitoring and tuning
5. **Graceful Degradation**: Provide meaningful error messages to clients
6. **Testing**: Thoroughly test filter combinations and edge cases

## Error Handling

Traffik provides specific exceptions for different error conditions:

### Exception Types

- **`TraffikException`** - Base exception for all Traffik-related errors
- **`ConfigurationError`** - Raised when throttle or backend configuration is invalid
- **`AnonymousConnection`** - Raised when connection identifier cannot be determined  
- **`ConnectionThrottled`** - HTTP 429 exception raised when rate limits are exceeded

### Exception Handling Examples

Handle configuration errors:

```python
from traffik.exceptions import ConfigurationError
from traffik.backends.redis import RedisBackend

try:
    backend = RedisBackend(connection="invalid://url")
    await backend.initialize()
except ConfigurationError as e:
    print(f"Backend configuration error: {e}")
```

Handle anonymous connections:

```python
from starlette.requests import HTTPConnection
from traffik.exceptions import AnonymousConnection
from traffik.backends.base import connection_identifier

async def safe_identifier(connection: HTTPConnection) -> str:
    try:
        return await connection_identifier(connection)
    except AnonymousConnection:
        return f"anonymous:{connection.scope['path']}"
```

Raise throttled exception:

```python
from traffik.exceptions import ConnectionThrottled

async def custom_throttle_handler(connection, wait_period, *args, **kwargs):
    raise ConnectionThrottled(
        wait_period=wait_period,
        detail=f"Rate limited. Retry in {wait_period}s",
        headers={"X-Custom-Header": "throttled"}
    )
```

## Testing

Here's an example of how to test throttling behavior using the `InMemoryBackend`. You can
write something similar for your custom backend too.

```python
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport # pip install httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse
from traffik.backends.inmemory import InMemoryBackend
from traffik.throttles import HTTPThrottle


@pytest.fixture(scope="function")
async def backend() -> InMemoryBackend:
    return InMemoryBackend(prefix="test", persistent=False)


@pytest.mark.anyio
async def test_throttling(backend: InMemoryBackend):
    throttle = HTTPThrottle(limit=2, seconds=1)

    async def throttled_endpoint(request: Request):
        """
        Endpoint that is throttled.
        """
        await throttle(request)
        return JSONResponse({"message": "Success"})
    
    app = Starlette(
        routes=[
            Route("/throttled", throttled_endpoint, methods=["GET"]),
        ],
        lifespan=backend.lifespan,  # Use `backend.lifespan` for cleanup
    )

    # Test that first 2 requests succeed
    async with AsyncClient(
        transport=ASGITransport(app), 
        base_url="http://127.0.0.1:123",
    ) as client:
        response1 = await client.get("/throttled")
        response2 = await client.get("/throttled") 
        assert response1.status_code == 200
        assert response2.status_code == 200
        
        # Third request should be throttled
        response3 = await client.get("/throttled")
        assert response3.status_code == 429
```

## API Reference

### Throttle Classes

#### `HTTPThrottle`

- **Purpose**: Rate limiting for HTTP requests
- **Usage**: As FastAPI dependency or decorator
- **Key Generation**: Based on route, client IP, and throttle instance

#### `WebSocketThrottle`  

- **Purpose**: Rate limiting for WebSocket connections
- **Usage**: Call directly in WebSocket handlers
- **Key Generation**: Based on WebSocket path, client, and optional context

#### `BaseThrottle`

- **Purpose**: Base class for custom throttle implementations
- **Customizable**: Override `get_key()` method for custom key generation

### Backend Classes

#### `InMemoryBackend`

- **Storage**: Python dictionary
- **Suitable for**: Development, testing, single-process apps
- **Persistence**: Optional (not recommended for production)

#### `RedisBackend`

- **Storage**: Redis database
- **Suitable for**: Production, multi-process, distributed systems
- **Persistence**: Built-in Redis persistence

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
