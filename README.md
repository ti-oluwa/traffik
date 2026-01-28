# Traffik - A Starlette/FastAPI throttling library

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)

Traffik provides flexible rate limiting for Starlette and FastAPI applications with support for both HTTP and WebSocket connections. It offers multiple rate limiting strategies including Fixed Window, Sliding Window, Token Bucket, and Leaky Bucket algorithms, allowing you to choose the approach that best fits your use case.

The library features pluggable backends (in-memory, Redis, Memcached), context-aware backend resolution for special applications. Whether you need simple per-endpoint limits or complex distributed rate limiting, Traffik provides the flexibility and robustness to handle your requirements.

Traffik was inspired by [fastapi-limiter](https://github.com/long2ice/fastapi-limiter) but has evolved into a more comprehensive solution with more advanced features.

## Features

- **Easy Integration**: Decorator, dependency, and middleware-based throttling
- **Multiple Strategies**: Fixed Window (default), Sliding Window, Token Bucket, Leaky Bucket
- **Multiple Backends**: In-memory, Redis, Memcached with atomic operation support
- **Protocol Support**: Both HTTP and WebSocket throttling
- **Context-Aware Backend Resolution**: Support for runtime backend switching
- **Dependency Injection Friendly**: Works well with FastAPI's dependency injection system.
- **Smart Client Identification**: IP-based by default, fully customizable
- **Middleware Support**: Global throttling via Starlette middleware
- **Flexible and Extensible API**: Easily extend base functionality

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

## Quick Start Guide

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
async def hello_endpoint():
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
async def endpoint():
    return {"data": "Limited access"}
```

### 4. WebSocket Throttling

```python
from traffik.throttles import WebSocketThrottle
from traffik.exceptions import ConnectionThrottled
from starlette.websockets import WebSocket
from starlette.exceptions import HTTPException

ws_throttle = WebSocketThrottle(uid="ws_messages", rate="3/10s")

async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    while True:
        data = await websocket.receive_json()
        # Throttle sends rate limit message to client, doesn't raise
        await ws_throttle(websocket)  # Rate limit per message
        await websocket.send_json({
            "status": "success",
            "data": data,
        })
    
    await websocket.close()


# Alternatively, you can provide a throttled handler that closes the connection
async def close_on_throttle(connection: WebSocket, wait_ms: WaitPeriod, *args, **kwargs):
    await connection.send_json({"error": "rate_limited"})
    await asyncio.sleep(0.1)  # Give time for message to be sent
    await connection.close(code=1008)  # Policy Violation

ws_throttle = WebSocketThrottle(
    uid="ws_messages",
    rate="3/10s",
    handle_throttled=close_on_throttle,
)
```

## Rate Limiting Strategies

Traffik supports multiple rate limiting strategies, each with different trade-offs. The **Fixed Window strategy is used by default** for its simplicity and performance.

### Fixed Window (Traffik Default)

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

## Writing Custom Strategies

You can create custom rate limiting strategies by implementing a callable that follows the strategy protocol. This allows you to implement specialized rate limiting logic tailored to your specific needs.

### Strategy Protocol

A strategy is a callable (function or class with `__call__`) that takes four parameters and returns a wait period:

```python
from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod

async def my_strategy(
    key: Stringable,
    rate: Rate,
    backend: ThrottleBackend,
    cost: int = 1,
) -> WaitPeriod:
    """
    :param key: The throttling key (e.g., "user:123", "ip:192.168.1.1")
    :param rate: Rate limit definition with limit and expire properties
    :param backend: Backend instance for storage operations
    :param cost: Cost of the request (default is 1)
    :return: Wait time in milliseconds (0.0 if request allowed)
    """
    # Your rate limiting logic here
    return 0.0  # Allow request
```

### Example: Simple Rate Strategy

Here's a basic example that implements a simple counter-based strategy:

```python
from dataclasses import dataclass
from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod
from traffik.utils import time


async def simple_counter_strategy(
    key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
) -> WaitPeriod:
    """
    Simple counter-based rate limiting.
    
    Counts requests and resets counter when expired.
    """
    if rate.unlimited:
        return 0.0
    
    now = time() * 1000  # Current time in milliseconds
    full_key = await backend.get_key(str(key))
    counter_key = f"{full_key}:counter"
    timestamp_key = f"{full_key}:timestamp"
    
    ttl_seconds = int(rate.expire // 1000) + 1
    
    async with await backend.lock(f"lock:{counter_key}", blocking=True, blocking_timeout=1):
        # Get current count and timestamp
        count_str = await backend.get(counter_key)
        timestamp_str = await backend.get(timestamp_key)
        
        if count_str and timestamp_str:
            count = int(count_str)
            timestamp = float(timestamp_str)
            
            # Check if window has expired
            if now - timestamp > rate.expire:
                # Reset counter for new window
                count = cost
                await backend.set(counter_key, str(count), expire=ttl_seconds)
                await backend.set(timestamp_key, str(now), expire=ttl_seconds)
            else:
                # Increment counter
                count = await backend.increment(counter_key, amount=cost)
        else:
            # First request
            count = cost
            await backend.set(counter_key, str(count), expire=ttl_seconds)
            await backend.set(timestamp_key, str(now), expire=ttl_seconds)
        
        # Check if limit exceeded
        if count > rate.limit:
            # Calculate wait time until window expires
            timestamp = float(await backend.get(timestamp_key))
            elapsed = now - timestamp
            wait_ms = rate.expire - elapsed
            return max(wait_ms, 0.0)
        
        return 0.0

# Usage
throttle = HTTPThrottle(
    uid="simple",
    rate="10/m",
    strategy=simple_counter_strategy,
)
```

### Example: Adaptive Rate Strategy

A more advanced example that adapts the rate limit based on backend load:

```python
from dataclasses import dataclass
from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod
from traffik.utils import time

@dataclass(frozen=True)
class AdaptiveRateStrategy:
    """
    Adaptive rate limiting that adjusts based on system load.
    
    Reduces limits during high load, increases during low load.
    """
    
    load_threshold: float = 0.8  # 80% of limit triggers adaptation
    reduction_factor: float = 0.5  # Reduce to 50% during high load
    
    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0
        
        now = time() * 1000
        window_duration_ms = rate.expire
        current_window = int(now // window_duration_ms)
        
        full_key = await backend.get_key(str(key))
        counter_key = f"{full_key}:adaptive:{current_window}"
        load_key = f"{full_key}:load"
        ttl_seconds = int(window_duration_ms // 1000) + 1
        
        async with await backend.lock(f"lock:{counter_key}", blocking=True, blocking_timeout=1):
            # Increment request counter by cost
            count = await backend.increment_with_ttl(counter_key, amount=cost, ttl=ttl_seconds)
            
            # Calculate current load percentage
            load_percentage = count / rate.limit
            
            # Determine effective limit based on load
            if load_percentage > self.load_threshold:
                # High load - reduce effective limit
                effective_limit = int(rate.limit * self.reduction_factor)
                await backend.set(load_key, "high", expire=ttl_seconds)
            else:
                # Normal load - use full limit
                effective_limit = rate.limit
                await backend.set(load_key, "normal", expire=ttl_seconds)
            
            # Check against effective limit
            if count > effective_limit:
                # Calculate wait time
                time_in_window = now % window_duration_ms
                wait_ms = window_duration_ms - time_in_window
                return wait_ms
            
            return 0.0

# Usage
throttle = HTTPThrottle(
    uid="adaptive_api",
    rate="1000/h",
    strategy=AdaptiveRateStrategy(load_threshold=0.7, reduction_factor=0.6)
)
```

### Example: Priority-Based Strategy

A strategy that implements priority queuing:

```python
from dataclasses import dataclass
from enum import IntEnum
from traffik.backends.base import ThrottleBackend
from traffik.rates import Rate
from traffik.types import Stringable, WaitPeriod
from traffik.utils import time, dump_json, load_json, JSONDecodeError

class Priority(IntEnum):
    """Request priority levels"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4

@dataclass(frozen=True)
class PriorityQueueStrategy:
    """
    Rate limiting with priority queue.
    
    Higher priority requests are processed first when at capacity.
    """
    
    default_priority: Priority = Priority.NORMAL
    
    def _extract_priority(self, key: str) -> Priority:
        """Extract priority from key (format: "priority:<level>:user:123")"""
        if key.startswith("priority:"):
            try:
                level = int(key.split(":")[1])
                return Priority(level)
            except (ValueError, IndexError):
                pass
        return self.default_priority
    
    async def __call__(
        self, key: Stringable, rate: Rate, backend: ThrottleBackend, cost: int = 1
    ) -> WaitPeriod:
        if rate.unlimited:
            return 0.0
        
        now = time() * 1000
        priority = self._extract_priority(str(key))
        full_key = await backend.get_key(str(key))
        queue_key = f"{full_key}:priority_queue"
        ttl_seconds = int(rate.expire // 1000) + 1
        
        async with await backend.lock(f"lock:{queue_key}", blocking=True, blocking_timeout=1):
            # Get current queue
            queue_json = await backend.get(queue_key)
            if queue_json:
                try:
                    queue = load_json(queue_json)
                except JSONDecodeError:
                    queue = []
            else:
                queue = []
            
            # Remove expired entries
            queue = [
                entry for entry in queue 
                if now - entry["timestamp"] < rate.expire
            ]
            # Count total cost of requests with higher or equal priority
            higher_priority_cost = sum(
                entry.get("cost", 1) for entry in queue 
                if entry["priority"] >= priority
            )
            
            # Check if request can be processed (including current request's cost)
            if higher_priority_cost + cost > rate.limit:
                # Calculate wait time based on oldest high-priority entry
                oldest_high_priority = min(
                    (entry["timestamp"] for entry in queue if entry["priority"] >= priority),
                    default=now
                )
                wait_ms = rate.expire - (now - oldest_high_priority)
                return max(wait_ms, 0.0)
            
            # Add current request to queue
            queue.append({
                "timestamp": now,
                "priority": priority,
                "key": str(key),
                "cost": cost
            })
            # Sort by priority (descending) and timestamp (ascending)
            queue.sort(key=lambda x: (-x["priority"], x["timestamp"]))
            # Store updated queue
            await backend.set(queue_key, dump_json(queue), expire=ttl_seconds)
            return 0.0

# Usage
from traffik.throttles import HTTPThrottle

async def priority_identifier(connection):
    """Extract priority from request headers"""
    priority = connection.headers.get("X-Priority", "2")
    user_id = extract_user_id(connection)
    return f"priority:{priority}:user:{user_id}"

throttle = HTTPThrottle(
    uid="priority_api",
    rate="100/m",
    strategy=PriorityQueueStrategy(),
    identifier=priority_identifier
)
```

### Best Practices for Custom Strategies

1. **Always use locks**: Wrap critical sections with `backend.lock()` to ensure atomicity

   ```python
   async with await backend.lock(f"lock:{key}", blocking=True, blocking_timeout=1):
       # Critical section
   ```

2. **Set appropriate TTLs**: Always set expiration times to prevent memory leaks

   ```python
   ttl_seconds = int(rate.expire // 1000) + 1  # +1 second buffer
   await backend.set(key, value, expire=ttl_seconds)
   ```

3. **Handle unlimited rates**: Check for unlimited rates early

   ```python
   if rate.unlimited:
       return 0.0
   ```

4. **Use proper key prefixes**: Namespace your strategy's keys to avoid conflicts

   ```python
   full_key = await backend.get_key(str(key))
   strategy_key = f"{full_key}:mystrategy:data"
   ```

5. **Return milliseconds**: Always return wait time in milliseconds

   ```python
   return wait_ms  # Not wait_seconds * 1000
   ```

6. **Use dataclasses**: Make strategies immutable and configurable

   ```python
   @dataclass(frozen=True)
   class MyStrategy:
       param1: int = 10
       param2: float = 0.5
   ```

7. **Handle errors gracefully**: Catch and handle JSON decode errors, type errors, etc.

   ```python
   try:
       data = load_json(json_str)
   except JSONDecodeError:
       data = default_value
   ```

8. **Document your strategy**: Provide clear documentation for your strategy's behavior, configuration options, and any caveats.

9. **Consider performance implications**: Be aware of the potential performance impact of your strategy, especially under high load. Avoid blocking operations (e.g, logging) and optimize data structures used for tracking requests.

### Testing Custom Strategies

```python
import pytest
from traffik.backends.inmemory import InMemoryBackend
from traffik.rates import Rate

@pytest.mark.anyio
async def test_custom_strategy():
    backend = InMemoryBackend(namespace="test")
    async with backend(close_on_exit=True):
        strategy = MyCustomStrategy()
        rate = Rate.parse("5/s")
        
        # Test allowing requests
        for i in range(5):
            wait = await strategy(f"user:123", rate, backend)
            assert wait == 0.0, f"Request {i+1} should be allowed"
        
        # Test throttling
        wait = await strategy(f"user:123", rate, backend)
        assert wait > 0, "Request 6 should be throttled"
        
        # Verify wait time is reasonable
        assert wait <= rate.expire, "Wait time should not exceed window"
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
import typing

from traffik.backends.base import ThrottleBackend
from traffik.types import HTTPConnectionT, AsyncLock

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
    
    async def get_lock(self, name: str) -> AsyncLock:
        """Get distributed lock"""
        pass
    
    async def reset(self) -> None:
        """Clear all data"""
        pass
    
    async def close(self) -> None:
        """Cleanup resources"""
        pass
```

> Note: Ensure that backend operations are as fast as possible, and non-blocking. Avoiding logging or heavy computations in backend methods is crucial for performance. Use `print` statements if absolutely necessary for debugging.

### Throttle Backend Selection

You can specify the backend for each throttle individually or allow them to share a common backend. The shared backend is usually the one set up in your application lifespan.

```python
from fastapi import FastAPI, Depends, Request

from traffik.backends.redis import RedisBackend
from traffik.throttles import HTTPThrottle

shared_redis_backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="shared",
)
app = FastAPI(lifespan=shared_redis_backend.lifespan)

throttle_with_own_backend = HTTPThrottle(
    uid="custom_backend",
    rate="100/m",
    backend=MyCustomBackend(),  # Uses its own backend
)
# Uses backend from app lifespan
throttle_with_shared_backend = HTTPThrottle(
    uid="shared_backend",
    rate="50/m",
)

@app.get("/api/custom", dependencies=[Depends(throttle_with_shared_backend)])
async def endpoint(request: Request = Depends(throttle_with_own_backend)):
    return {"message": "Uses its own backend"}

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

async def get_user_id(connection: HTTPConnection) -> str:
    """Identify by user ID from JWT token"""
    user_id = extract_user_id(connection.headers.get("authorization"))
    return f"user:{user_id}"

throttle = HTTPThrottle(
    uid="user_limit",
    rate="100/h",
    identifier=get_user_id,
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

### Weighted Requests (Request Costs)

Assign different costs to requests based on their resource intensity. This allows you to count expensive operations more heavily against rate limits:

```python
from fastapi import FastAPI, Request, Depends
from traffik.throttles import HTTPThrottle

# Default cost of 1 per request
standard_throttle = HTTPThrottle(
    uid="standard",
    rate="100/m",
)

# Expensive operations cost more
expensive_throttle = HTTPThrottle(
    uid="reports",
    rate="100/m",
    cost=5,  # Each request counts as 5 toward the limit
)

# Per-request cost override
async def dynamic_endpoint(request: Request):
    # Check operation type
    if request.query_params.get("full_export"):
        # Override with higher cost for expensive operation
        await expensive_throttle(request, cost=10)
    else:
        await expensive_throttle(request, cost=2)
    return {"status": "ok"}

@app.get("/api/data", dependencies=[Depends(standard_throttle)])
async def get_data():
    return {"data": "value"}

@app.get("/api/reports", dependencies=[Depends(expensive_throttle)])
async def generate_report():
    return {"report": "data"}

@app.post("/api/export")
async def export_data(request: Request):
    return await dynamic_endpoint(request)
```

**Use cases for request costs:**

- Heavy database queries (cost=5)
- Report generation (cost=10)
- File uploads (cost based on file size)
- AI/ML inference requests (cost=20)
- Bulk operations (cost proportional to batch size)

```python
# Example: Cost based on request size
async def size_based_cost(request: Request):
    content_length = int(request.headers.get("content-length", 0))
    # 1 cost per MB
    cost = max(1, content_length // (1024 * 1024))
    await upload_throttle(request, cost=cost)

upload_throttle = HTTPThrottle(uid="uploads", rate="1000/h")
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
    # Uses `FixedWindowStrategy` by default
)
```

### Context-Aware (Dynamic) Backend Resolution

Here, let's look at an example where we enable runtime backend switching for multi-tenant SaaS applications:

```python
from fastapi import FastAPI, Request, Depends
from traffik.throttles import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.backends.inmemory import InMemoryBackend
import jwt

# Shared throttle with context-aware backend resolution
api_throttle = HTTPThrottle(
    uid="api_quota",
    rate="1000/h",
    context_backend=True,  # Resolve backend at runtime based on request context
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
    async with backend(request.app):
        return await call_next(request)

app = FastAPI()
app.middleware("http")(tenant_middleware)

@app.get("/api/data")
async def get_data(request: Depends(api_throttle)):
    return {"data": "tenant-specific data"}
```

**When to use context-aware backends:**

- ✅ Multi-tenant SaaS with per-tenant storage
- ✅ A/B testing different strategies
- ✅ Environment-specific backends
- ❌ Simple shared storage (use explicit `backend` parameter instead)

**Important:** Context-aware backend resolution adds slight overhead and complexity. Only use when you need runtime backend switching.

### Application Lifespan Management

It is necessary to manage the lifecycle of backends properly to ensure resources are cleaned up and connections are closed. To do this, integrate the backend's lifespan into your application. If you have a custom lifespan, make sure to include the backend's lifespan within it.

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
    async with backend(app, persistent=True, close_on_exit=True):
        yield
    # Shutdown - backend cleanup automatic

app = FastAPI(lifespan=lifespan)
```

## Throttle Middleware

With throttle middleware, you can apply rate limiting across multiple endpoints with sophisticated filtering and routing logic.

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

Throttle differently based on HTTP methods:

```python
# Strict limits for write operations
write_throttle = HTTPThrottle(uid="writes", rate="10/m")
read_throttle = HTTPThrottle(uid="reads", rate="1000/m")

write_middleware_throttle = MiddlewareThrottle(
    write_throttle,
    methods={"POST", "PUT", "DELETE"}
)

read_middleware_throttle = MiddlewareThrottle(
    read_throttle,
    methods={"GET", "HEAD"}
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[write_middleware_throttle, read_middleware_throttle],
    backend=backend # If not set, uses app lifespan backend
)
```

#### Path Pattern Filtering

Throttle based on URL path patterns:

```python
# String patterns and regex
api_throttle = HTTPThrottle(uid="api", rate="100/m")
admin_throttle = HTTPThrottle(uid="admin", rate="5/m")

api_middleware_throttle = MiddlewareThrottle(
    api_throttle,
    path="/api/"  # Starts with /api/
)
admin_middleware_throttle = MiddlewareThrottle(
    admin_throttle,
    path=r"^/admin/.*"  # Regex pattern
)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[admin_middleware_throttle, api_middleware_throttle],
    backend=backend
)
```

#### Custom Predicate-Based Filtering

Throttle based on custom logic:

```python
from starlette.requests import HTTPConnection

async def is_authenticated(connection: HTTPConnection) -> bool:
    """Apply throttle only to authenticated users"""
    return connection.headers.get("authorization") is not None

async def is_intensive_op(connection: HTTPConnection) -> bool:
    """Throttle resource-intensive endpoints"""
    intensive_paths = ["/api/reports/", "/api/analytics/", "/api/exports/"]
    return any(connection.scope["path"].startswith(p) for p in intensive_paths)

auth_throttle = HTTPThrottle(uid="auth", rate="200/m")
intensive_throttle = HTTPThrottle(uid="intensive", rate="20/m")

auth_middleware_throttle = MiddlewareThrottle(auth_throttle, predicate=is_authenticated)
intensive_middleware_throttle = MiddlewareThrottle(intensive_throttle, predicate=is_intensive_op)

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[intensive_middleware_throttle, auth_middleware_throttle],
    backend=backend
)
```

#### Combined Filtering

Combine path, method, and hook criteria:

```python
async def is_authenticated(connection: HTTPConnection) -> bool:
    return connection.headers.get("authorization") is not None

complex_throttle = HTTPThrottle(uid="complex", rate="25/m")

# Combines ALL criteria: path AND method AND hook
complex_middleware_throttle = MiddlewareThrottle(
    complex_throttle,
    path="/api/",
    methods={"POST"},
    predicate=is_authenticated
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
        admin_middleware_throttle,      # Most specific
        intensive_middleware_throttle,  # Specific paths
        api_middleware_throttle,        # General API
    ],
    backend=redis_backend
)
```

## Error Handling

Traffik raises specific exceptions for different error scenarios:

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

The `ConnectionThrottled` exception is raised when a client exceeds their rate limit. It is an `HTTPException` so its is handled automatically by FastAPI/Starlette, returning a `429 Too Many Requests` response. However, you can still register a custom exception handler if you want to customize the response further.

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

A `ConfigurationError` is raised when there is an invalid configuration in your throttle or backend setup. You can catch this exception during application startup to log or handle misconfigurations.

```python
from traffik.exceptions import ConfigurationError, BackendConnectionError

try:
    backend = RedisBackend(connection="invalid://url")
    await backend.initialize()
except BackendConnectionError as exc:
    logger.error(f"Failed to connect to Redis: {exc}")
except ConfigurationError as exc:
    logger.error(f"Invalid configuration: {exc}")
```

### Handling Anonymous Connections

The default identifier raises `AnonymousConnection` if it cannot identify the client. You can catch this exception to provide a fallback identifier. Better still, implement a custom identifier function that handles anonymous clients gracefully.

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
    async with backend():
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
    context_backend: bool = False,     # Runtime context-aware backend resolution
    min_wait_period: Optional[int] = None,  # Minimum wait (ms)
    headers: Optional[Dict[str, str]] = None,  # Extra headers to include in throttled response
)
```

#### `WebSocketThrottle`

WebSocket message rate limiting.

```python
WebSocketThrottle(
    uid: str,
    rate: Union[Rate, str],
    identifier: Optional[Callable],
    handle_throttled: Optional[Callable],
    strategy: Optional[Strategy],
    backend: Optional[ThrottleBackend],
    context_backend: bool = False,
    min_wait_period: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
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
uv run ruff check src/ tests/ --fix

# Run formatting  
uv run ruff format src/ tests/
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history and changes.
