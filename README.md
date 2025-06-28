# Traffik - A Starlette throttling library

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
<!-- [![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/) -->

Traffik provides rate limiting capabilities for starlette-based applications like FastAPI with support for both HTTP and WebSocket connections. It offers multiple backend options including in-memory storage for development and Redis for production environments. Customizable throttling strategies allow you to define limits based on time intervals, client identifiers, and more.

Traffik was inspired by [fastapi-limiter](https://github.com/long2ice/fastapi-limiter), and some of the code is adapted from it. However, Traffik aims to provide a more flexible and extensible solution with a focus on ease of use. Some of these differences are listed below.

## Features

- 🚀 **Easy Integration**: Simple decorator and dependency-based throttling
- 🔄 **Multiple Backends**: In-memory (development) and Redis (production) support
- 🌐 **Protocol Support**: Both HTTP and WebSocket throttling
- 🔧 **Flexible Configuration**: Time-based limits with multiple time units
- 🎯 **Per-Route Throttling**: Individual limits for different endpoints
- 📊 **Client Identification**: Customizable client identification strategies
****
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

WebSocket throttling can be implemented using the `WebSocketThrottle` class. This allows you to limit the number of messages a client can send over a WebSocket connection.

```python
from traffik.throttles import WebSocketThrottle
from starlette.websockets import WebSocket

ws_throttle = WebSocketThrottle(
    limit=3,
    seconds=10,
)

async def ws_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint with throttling.
    """
    await websocket.accept()
    print("ACCEPTED WEBSOCKET CONNECTION")
    close_code = 1000  # Normal closure
    close_reason = "Normal closure"
    while True:
        try:
            data = await websocket.receive_json()
            await ws_throttle(websocket)
            await websocket.send_json(
                {
                    "status": "success",
                    "status_code": 200,
                    "headers": {},
                    "detail": "Request successful",
                    "data": data,
                }
            )
        except HTTPException as exc:
            print("HTTP EXCEPTION:", exc)
            await websocket.send_json(
                {
                    "status": "error",
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                    "headers": exc.headers,
                    "data": None,
                }
            )
            close_reason = exc.detail
            break
        except Exception as exc:
            print("WEBSOCKET ERROR:", exc)
            await websocket.send_json(
                {
                    "status": "error",
                    "status_code": 500,
                    "detail": "Operation failed",
                    "headers": {},
                    "data": None,
                }
            )
            close_code = 1011  # Internal error
            close_reason = "Internal error"
            break
    await websocket.close(code=close_code, reason=close_reason)
```

You can then use this WebSocket endpoint in your Starlette application:

```python
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

app = Starlette(
    routes=[
        WebSocketRoute("/ws/limited", ws_endpoint),
    ]
)
```

Or in a FastAPI application:

```python
from fastapi import FastAPI, WebSocket

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

You can create custom backends by subclassing `ThrottleBackend` and implementing the required methods. This allows you to integrate with any storage system or caching layer. Let's look at an example of a custom SQLite backend.

```python
import typing
import time
import asyncio
from contextlib import asynccontextmanager
from traffik.backends.base import ThrottleBackend
from traffik.types import (
    HTTPConnectionT,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
)
from traffik.exceptions import ConfiguationError
from aiosqlite import connect, Connection


class SQLiteBackend(ThrottleBackend[Connection, HTTPConnectionT]):
    """
    SQLite-based throttle backend for persistent rate limiting.
    
    Suitable for single-process applications that need persistence
    across restarts without requiring Redis infrastructure.
    """
    
    def __init__(
        self, 
        connection_string: str = ":memory:", 
        *,
        prefix: str = "sqlite-throttle",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ):
        # Store connection string, don't create connection yet
        self._connection_string = connection_string
        self._connection: typing.Optional[Connection] = None
        super().__init__(
            connection=None,  # Will be set in initialize()
            prefix=prefix,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )

    async def initialize(self) -> None:
        """Initialize SQLite database and create required tables."""
        self._connection = await aiosqlite.connect(self._connection_string)
        self.connection = self._connection  # Set for base class
        
        # Enable WAL mode for better concurrency
        await self._connection.execute("PRAGMA journal_mode=WAL")
        
        # Create throttles table with proper indexing
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS throttle_records (
                key TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 1,
                window_start INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        
        # Index for efficient cleanup of expired records
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires_at 
            ON throttle_records(expires_at)
        """)
        
        await self._connection.commit()
        
        # Start cleanup task for expired records
        asyncio.create_task(self._cleanup_expired_records())

    async def get_wait_period(self, key: str, limit: int, expires_after: int) -> int:
        """
        Check throttle status and return wait period if throttled.
        
        :param key: Throttle key (includes prefix)
        :param limit: Maximum requests allowed
        :param expires_after: Window duration in milliseconds 
        :return: Wait period in milliseconds (0 if not throttled)
        """
        if not self._connection:
            raise ConfiguationError("Backend not initialized")
            
        current_time_ms = int(time.time() * 1000)
        window_start = current_time_ms
        expires_at = current_time_ms + expires_after
        
        # Use a transaction for atomic read-modify-write
        async with self._connection.execute("BEGIN IMMEDIATE"):
            # Clean up expired record for this key
            await self._connection.execute(
                "DELETE FROM throttle_records WHERE key = ? AND expires_at <= ?",
                (key, current_time_ms)
            )
            
            # Get current count or insert new record
            cursor = await self._connection.execute(
                """
                INSERT INTO throttle_records (key, count, window_start, expires_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    count = count + 1
                RETURNING count, window_start, expires_at
                """,
                (key, window_start, expires_at)
            )
            
            row = await cursor.fetchone()
            if not row:
                await self._connection.commit()
                return 0
                
            count, record_window_start, record_expires_at = row
            await self._connection.commit()
            
            # Check if limit exceeded
            if count > limit:
                # Calculate remaining wait time
                wait_time = record_expires_at - current_time_ms
                return max(0, wait_time)
                
        return 0

    async def reset(self) -> None:
        """Reset all throttle records."""
        if not self._connection:
            return
        
        # Default key pattern is "{self.prefix}:*"
        pattern = str(self.key_pattern).replace("*", "%")
        await self._connection.execute(
            "DELETE FROM throttle_records WHERE key LIKE ?",
            (pattern,)
        )
        await self._connection.commit()

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _cleanup_expired_records(self) -> None:
        """Background task to periodically clean up expired records."""
        while self._connection:
            try:
                current_time = int(time.time() * 1000)
                await self._connection.execute(
                    "DELETE FROM throttle_records WHERE expires_at <= ?",
                    (current_time,)
                )
                await self._connection.commit()
                
                # Clean up every 60 seconds
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Log error in production
                print(f"Cleanup task error: {exc}")
                await asyncio.sleep(60)
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

### Excluding connections from Throttling

You can exclude certain connections from throttling by writing a custom identifier that raises `traffik.exceptions.NoLimit` for those connections. This is useful when you have throttles you want to skip for specific routes or clients.

```python
from starlette.requests import HTTPConnection
from traffik.exceptions import NoLimit

async def admin_identifier(connection: HTTPConnection):
    # Use user ID from JWT token
    user_id = extract_user_id(connection.headers.get("authorization"))
    if user_id == "admin":
        raise NoLimit()  # Skip throttling for admin users
    return f"user:{user_id}:{connection.scope['path']}"

throttle = HTTPThrottle(
    limit=10,
    minutes=1,
    identifier=admin_identifier,  # Override default (backend) identifier
)
```

### Custom Throttled Response

Customize what happens when a client is throttled:

```python
from starlette.requests import HTTPConnection
from starlette.exceptions import HTTPException
import traffik


async def custom_throttled_handler(connection: HTTPConnection, wait_period: int):
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


async def user_identifier(request: Request):
    # Extract user ID from JWT or session
    user_id = await get_user_id(request)
    return f"user:{user_id}"


user_throttle = HTTPThrottle(
    limit=100,
    hours=1,
    identifier=user_identifier,
)
```

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

## Error Handling

Traffik provides specific exceptions for different error conditions:

A `traffik.exceptions.ConfigurationError` is raised when a throttle or throttle backend configuration is invalid.

```python
from starlette.requests import HTTPConnection
from traffik.exceptions import AnonymousConnection
from traffik import connection_identifier # Default identifier function


# Custom identifier that handles anonymous users
async def safe_identifier(connection: HTTPConnection):
    """
    Safely get the connection identifier, handling anonymous connections.
    """
    try:
        return connection_identifier(connection)
    except AnonymousConnection:
        # Fallback for anonymous connections
        return f"anonymous:{connection.scope['path']}"
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

## Performance Considerations

### Redis Backend Implementation

The Redis backend uses optimized Lua scripts to minimize round trips:

```lua
-- Atomic increment with expiration
local current = redis.call('GET', key) or "0"
if current + 1 > limit then
    return redis.call("PTTL", key)  -- Return remaining time
else
    redis.call("INCR", key)
    if current == "0" then
        redis.call("PEXPIRE", key, expire_time)
    end
    return 0  -- Allow request
end
```

### In-Memory Backend Implementation

Uses efficient dictionary lookups with monotonic time for accuracy:

```python
now = int(time.monotonic() * 1000)  # Monotonic milliseconds
record = store.get(key, {"count": 0, "start": now})
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
