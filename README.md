# Traffik

Asynchronous distributed rate limiting for FastAPI/Starlette applications.

[![Test](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/test.yaml)
[![Code Quality](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml/badge.svg)](https://github.com/ti-oluwa/traffik/actions/workflows/code-quality.yaml)
[![codecov](https://codecov.io/gh/ti-oluwa/traffik/branch/main/graph/badge.svg)](https://codecov.io/gh/ti-oluwa/traffik)
[![PyPI version](https://badge.fury.io/py/traffik.svg)](https://badge.fury.io/py/traffik)
[![Python versions](https://img.shields.io/pypi/pyversions/traffik.svg)](https://pypi.org/project/traffik/)

## Features

Rate limiting doesn't have to be complicated. Traffik gives you the tools to protect your APIs with minimal code and maximum flexibility:

- **Fully Asynchronous**: Built for async/await with non-blocking operations and minimal overhead/latency
- **Distributed-First**: Atomic operations with distributed locks (Redis, Memcached) achieving very high accuracy even under high concurrency.
- **10+ Strategies**: Fixed Window, Sliding Window (Log & Counter), Token Bucket, Leaky Bucket, GCRA, Adaptive, Tiered, Priority Queue, and more
- **HTTP & WebSocket**: Full-featured rate limiting for both protocols with per-message throttling support
- **Production-Ready**: Circuit breakers, automatic retries, backend failover, and custom error handling
- **Flexible Integration**: Dependencies, decorators, middleware, or direct calls - use what fits your architecture
- **Highly Extensible**: Simple, well-documented APIs for custom backends, strategies, error handlers, and identifiers
- **Observable**: Rich error context, and strategy statistics for monitoring
- **Performance-Optimized**: Lock striping, connection pooling, script caching, and minimal memory footprint

Built for production workloads with battle-tested patterns from high-scale systems.

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
- [Benchmarks](#benchmarks)
- [Performance](#performance)
- [API Reference](#api-reference)

## Installation

Getting started with Traffik is straightforward. Install it using your preferred package manager:

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

Let's get you up and running in under a minute. The examples below show just how little code you need to add rate limiting to your application.

### Minimal Example (5 lines)

```python
from fastapi import FastAPI, Depends
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(namespace="api")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle(uid="basic", rate="100/minute", backend=backend)

@app.get("/", dependencies=[Depends(throttle)])
async def root():
    return {"message": "ok"}
```

**What this does:**

- Allows 100 requests per minute per IP address
- Returns HTTP 429 (Too Many Requests) when exceeded
- Automatically includes `Retry-After` header
- No external storage dependencies (uses in-memory storage)
- Dev environment ready

### Example Setup for Production

When you're ready to deploy, here's a more complete setup with Redis, circuit breakers, and fallback protection. This is the kind of setup you'd use in a real production environment.

#### API Routes

```python
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend
from traffik.strategies import SlidingWindowCounterStrategy
from traffik.error_handlers import failover, CircuitBreaker
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
    on_error=failover(
        backend=fallback_backend,
        breaker=breaker,
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
from traffik.error_handlers import failover, CircuitBreaker
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

ws_throttle = WebSocketThrottle(
    uid="ws",
    rate="1000/hour",
    strategy=SlidingWindowCounterStrategy(),
    backend=backend,
    on_error=failover(
        backend=fallback_backend,
        breaker=breaker,
        max_retries=2,
    ),
)

@app.websocket("/ws/data")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    close_code = 1000
    reason = "Normal closure"
    while True:
        try:
            data = await websocket.receive_json()
            # Hit throttle. Default handler sends a throttle response if limit reached
            await ws_throttle(websocket, context={"scope": "<some_scope>"})
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

Before diving into the advanced features, it's worth understanding the building blocks that make Traffik work. These concepts form the foundation of how rate limiting is configured and applied.

### Rate Format

Trafik offers flexible ways to define your rate limits. Whether you prefer terse shorthand or explicit configuration, there's a format that fits your style:

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

Backends are where Traffik keeps track of request counts and timestamps. Your choice of backend depends on how your application is deployed—single server, multiple instances, or a distributed system. Here's a quick comparison to help you choose:

| Backend | Use Case | Persistence | Distributed | Overhead |
|---------|----------|-------------|-------------|----------|
| **InMemory** | Development, testing, single-process | No | No | Minimal |
| **Redis** | Production, multi-instance | Yes | Yes | Low |
| **Memcached** | High-throughput, caching | Yes | Yes | Low |

#### InMemory Backend

The simplest option—perfect for local development, testing, and single-process deployments. Zero external dependencies, instant setup.

```python
from traffik.backends.inmemory import InMemoryBackend

backend = InMemoryBackend(
    namespace="app",
    persistent=False,  # Don't persist across app restarts
    number_of_shards=16,  # Lock striping for concurrency
    cleanup_frequency=5.0,  # Cleanup expired keys every 5s
    # Lock configuration (optional)
    lock_ttl=5.0,  # Auto-release locks after 5 seconds
    lock_blocking=True,  # Wait for locks (default)
    lock_blocking_timeout=2.0,  # Max wait time for locks
)
```

**Characteristics:**

- Lock striping with configurable shards (default: 3)
- Automatic cleanup of expired keys
- Not suitable for multi-process deployments
- Data lost on restart even with `persistent=True`

#### Redis Backend

The go-to choice for production deployments. Persistent, distributed, and battle-tested. Supports both single-instance and multi-cluster setups with Redlock.

```python
from traffik.backends.redis import RedisBackend

# From URL
backend = RedisBackend(
    connection="redis://localhost:6379/0",
    namespace="app",
    persistent=True,
    lock_type="redis",  # or "redlock" for distributed/multi-cluster redis at the cost of latency
    # Lock configuration (optional)
    lock_ttl=5.0,  # Auto-release locks after 5 seconds
    lock_blocking=True,  # Wait for locks (default)
    lock_blocking_timeout=2.0,  # Max wait time for locks
)

# From factory
import redis.asyncio as redis

async def get_redis():
    return await redis.from_url("redis://localhost:6379/0")

backend = RedisBackend(
    connection=get_redis, namespace="app"
)
```

**Lock Types:**

- `"redis"`: Single Redis instance, low latency (~0.4-0.5ms overhead)
- `"redlock"`: Distributed Redlock algorithm, higher latency (~5ms overhead)

**Characteristics:**

- Lua scripts for atomic operations
- Script caching with NOSCRIPT recovery
- Persistent across restarts
- Production-ready

#### Memcached Backend

If you're already running Memcached for caching, you can use it for rate limiting too. Great for high-throughput scenarios where eventual consistency is acceptable.

```python
from traffik.backends.memcached import MemcachedBackend

backend = MemcachedBackend(
    host="localhost",
    port=11211,
    namespace="app",
    pool_size=10,
    pool_minsize=2,
    persistent=False,
    # Lock configuration (optional)
    lock_ttl=5.0,  # Auto-release locks after 5 seconds
    lock_blocking=True,  # Wait for locks (default)
    lock_blocking_timeout=2.0,  # Max wait time for locks
)
```

**Characteristics:**

- Best-effort distributed locks
- No native persistence/non-persistence guarantees. Use `track_keys=True` for better cleanup (at the cost of some latency).
- Good for high-throughput scenarios

**Important:** Memcached has no `KEYS` command. The `clear()` method is a no-op unless `track_keys=True` is enabled but this adds overhead.

### Strategies

Not all rate limiting algorithms are created equal. Each strategy makes different tradeoffs between accuracy, memory usage, and burst tolerance. Understanding these tradeoffs helps you pick the right one for your use case:

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
- Can allow up to 2x limit at boundaries (99 at 00:59, 100 at 01:00)

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

#### GCRA (Generic Cell Rate Algorithm)

```python
from traffik.strategies import GCRAStrategy

HTTPThrottle(
    uid="telecom",
    rate="100/minute",
    strategy=GCRAStrategy(burst_tolerance_ms=500)
)
```

**How it works:**

- Tracks Theoretical Arrival Time (TAT) for each request
- Enforces precise inter-request spacing
- More memory-efficient than token bucket (single timestamp vs. state object)
- Burst tolerance controls allowed variance from perfect spacing

**Storage:**

- `{key}:gcra:tat` - Single timestamp (most efficient)

**When to use:**

- Telecommunications/real-time systems
- Strict SLA enforcement requiring smooth traffic
- Preventing sudden load spikes
- Financial APIs with precise timing requirements

**Configuration:**

```python
# Perfectly smooth (no bursts)
GCRAStrategy(burst_tolerance_ms=0)

# Allow small bursts (500ms tolerance)
GCRAStrategy(burst_tolerance_ms=500)
```

### Identifiers

Identifiers are how Traffik knows who's who. By default, rate limits are applied per IP address, but real applications often need something more nuanced—maybe you want to limit by user account, API key, or tenant. An identifier function takes an `HTTPConnection` and returns a string that uniquely identifies the client.

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

Traffik's throttles can be integrated in multiple ways depending on your architecture.

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

Some developers prefer seeing the rate limit configuration right at the endpoint definition. The `@throttled` decorator provides that clean, declarative syntax while doing the same thing as dependencies under the hood:

```python
from traffik.decorators import throttled

# Single throttle
@app.get("/limited")
@throttled(HTTPThrottle(uid="limited", rate="5/minute"))
async def limited():
    return {"data": "limited"}

# Multiple throttles (all enforced)
burst = HTTPThrottle(uid="burst", rate="10/minute")
sustained = HTTPThrottle(uid="sustained", rate="100/hour")

@app.post("/create")
@throttled(burst, sustained)
async def create_resource():
    return {"status": "created"}

# Equivalent to:
# @app.get("/limited", dependencies=[Depends(throttle)])
# Or for multiple:
# @app.post("/create", dependencies=[Depends(burst), Depends(sustained)])
```

> **Note:** When using multiple throttles with `@throttled()`, all limits are checked sequentially before the request proceeds. If any throttle is exceeded, the request is rejected immediately without checking the remaining throttles.

### Middleware

Traffik's middleware allows applying throttles globally or conditionally based on path, method, or custom predicates.

> Note: Middleware only works with `HTTPThrottle`, not `WebSocketThrottle`.

```python
import re

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

# Regex path patterns

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        # String patterns (auto-compiled as regex)
        MiddlewareThrottle(
            HTTPThrottle(uid="api_v1", rate="100/minute"),
            path="/api/v1/"  # Matches /api/v1/*
        ),
        # Explicit regex patterns
        MiddlewareThrottle(
            HTTPThrottle(uid="user_endpoints", rate="50/minute"),
            path=re.compile(r"/api/users/\d+")  # Matches /api/users/123, etc.
        ),
        MiddlewareThrottle(
            HTTPThrottle(uid="file_downloads", rate="10/minute"),
            path=re.compile(r"/files/.*\.(pdf|zip|tar\.gz)$")  # Specific file types
        ),
    ],
)

# Method-based: Apply based on HTTP method
write_throttle = HTTPThrottle(uid="writes", rate="10/minute")
read_throttle = HTTPThrottle(uid="reads", rate="1000/minute")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(write_throttle, methods={"POST", "PUT", "DELETE"}),
        MiddlewareThrottle(read_throttle, methods={"GET", "HEAD"}),
    ],
)

# Predicate-based: Only apply if predicate is True
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

# Combined: Only for authenticated POSTs to /api/
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

Sometimes you need fine-grained control over when and how rate limiting is applied. Direct invocation lets you call the throttle programmatically, apply custom costs mid-request, or handle throttling as part of a larger workflow:

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

Once you've got the basics down, Traffik offers a rich set of features for handling real-world complexity—from variable request costs to multi-tier rate limits to sophisticated error recovery.

### Request Costs

Not all requests are equal. A file upload consumes more resources than a simple GET, and a complex report generation shouldn't count the same as a health check. Traffik lets you assign different costs to different operations:

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

Real APIs often need multiple layers of protection—a short-term burst limit to prevent sudden spikes, combined with a longer-term sustained limit to ensure fair usage over time. Traffik makes it easy to stack these limits:

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

Some users shouldn't be rate limited—premium customers, internal services, or trusted partners. Rather than building separate code paths, you can use the `EXEMPTED` sentinel value in your identifier function to gracefully bypass limits:

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

In multi-tenant SaaS applications, you might need different backends for different customers—enterprise clients get their dedicated Redis instance, while free-tier users share an in-memory store. Dynamic backend selection lets you make this decision at request time based on JWT claims, headers, or any other context.

**When to use `dynamic_backend=True`:**

| Scenario | Use `dynamic_backend` | Why |
|----------|----------------------|-----|
| Multi-tenant SaaS (tenant from JWT) | Yes | Tenant unknown until request arrives |
| A/B testing different storage | Yes | Selection based on request attributes |
| Shared Redis across services | No | Use explicit `backend` parameter |
| Single backend for all requests | No | Use explicit `backend` parameter |

**Complete Multi-Tenant Example:**

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends import RedisBackend, InMemoryBackend

app = FastAPI()

# Step 1: Create throttle with dynamic_backend=True
# No backend specified - it will be resolved from context at runtime
api_throttle = HTTPThrottle(
    uid="api",
    rate="1000/hour",
    dynamic_backend=True,
)

# Step 2: Middleware sets up tenant-specific backend BEFORE throttle runs
@app.middleware("http")
async def tenant_backend_middleware(request: Request, call_next):
    # Extract tenant from auth (JWT, API key, subdomain, etc.)
    tenant_id = request.headers.get("X-Tenant-ID", "default")
    tenant_tier = get_tenant_tier(tenant_id)  # Your lookup logic
    
    # Select backend based on tenant tier
    if tenant_tier == "enterprise":
        # Enterprise: Dedicated Redis instance
        backend = RedisBackend(f"redis://enterprise-redis:6379/0")
    elif tenant_tier == "premium":
        # Premium: Shared Redis with tenant namespace
        backend = RedisBackend(
            "redis://premium-redis:6379/0",
            namespace=f"tenant:{tenant_id}"
        )
    else:
        # Free tier: In-memory with tenant namespace
        backend = InMemoryBackend(namespace=f"free:{tenant_id}")
    
    # Enter backend context before request handlers run.
    # close_on_exit=False keeps connection alive for connection pooling
    # persistent=True maintains backend/throttling state across requests
    async with backend(request.app, close_on_exit=False, persistent=True):
        return await call_next(request)

# Step 3: Use throttle in route - it automatically uses the context backend
@app.get("/api/data")
async def get_data(request: Request = Depends(api_throttle)):
    return {"data": "tenant-specific rate limiting applied"}

# Helper function (your implementation)
def get_tenant_tier(tenant_id: str) -> str:
    # Look up tenant tier from database, cache, etc.
    tiers = {"acme-corp": "enterprise", "startup-x": "premium"}
    return tiers.get(tenant_id, "free")
```

**How it works:**

1. Request arrives → middleware extracts tenant info
2. Middleware creates appropriate backend and enters its context
3. Route handler runs → throttle resolves backend from current context
4. Throttle applies rate limit using tenant-specific backend
5. Request completes → context exits (backend connection managed by pool)

**Anti-pattern - Don't use for simple shared backends:**

```python
# Bad: Unnecessary dynamic resolution overhead
api_throttle = HTTPThrottle(uid="api", rate="1000/h", dynamic_backend=True)

# Good: Explicit backend for shared storage
shared_redis = RedisBackend("redis://shared-redis:6379/0")
api_throttle = HTTPThrottle(uid="api", rate="1000/h", backend=shared_redis)
```

**Performance impact:** ~1-20ms overhead per request for backend resolution, depending on backend initialization/connection speed.

## Configuration

### Global Settings

Traffik provides utilities to configure global defaults for lock behavior:

```python
from traffik import (
    set_lock_ttl,
    set_lock_blocking,
    set_lock_blocking_timeout,
    get_lock_ttl,
    get_lock_blocking,
    get_lock_blocking_timeout,
)

# Configure global lock blocking behavior
set_lock_ttl(5.0)  # All locks auto-release after 5 seconds (except specified otherwise)
set_lock_blocking(True)  # Wait for locks (default)
set_lock_blocking_timeout(2.0)   # Wait max 2 seconds for locks

# Or via environment variables
import os

os.environ["TRAFFIK_DEFAULT_LOCK_TTL"] = "5.0"
os.environ["TRAFFIK_DEFAULT_BLOCKING"] = "true"
os.environ["TRAFFIK_DEFAULT_BLOCKING_TIMEOUT"] = "2.0"

# Read current settings
ttl = get_lock_ttl()              # Returns float or None
blocking = get_lock_blocking()      # Returns bool
timeout = get_lock_blocking_timeout()       # Returns float or None
```

**Lock blocking settings:**

- `blocking=True`: Wait for lock acquisition (prevents lost updates)
- `blocking=False`: Fail immediately if lock unavailable (faster, may lose accuracy)
- `blocking_timeout`: Maximum wait time in seconds (prevents deadlocks)

**When to configure:**

| Scenario | Blocking | Timeout | Reason |
|----------|----------|---------|--------|
| High-accuracy required | True | 2.0s | Ensure atomicity |
| Low-latency priority | False | N/A | Fail fast |
| High concurrency | True | 0.5s | Prevent cascading waits |
| Development/testing | True | 5.0s | Allow debugging |

**Strategy-level overrides:**

Strategies can override global settings:

```python
from traffik.strategies import FixedWindowStrategy
from traffik.types import LockConfig

strategy = FixedWindowStrategy(
    lock_config=LockConfig(
        ttl=10.0,  # Override global TTL
        blocking=True,
        blocking_timeout=1.0,  # Override global timeout
    )
)
```

**Environment variables:**

- `TRAFFIK_DEFAULT_LOCK_TTL`: Float value in seconds (e.g., "5.0")
- `TRAFFIK_DEFAULT_BLOCKING`: "true", "false", "1", "0", "yes", "no"
- `TRAFFIK_DEFAULT_BLOCKING_TIMEOUT`: Float value in seconds (e.g., "2.0")

### Lock Contention and Sub-Second Windows

If you're pushing Traffik hard—think high concurrency, sub-second rate windows, or distributed deployments—understanding lock behavior becomes important. This section explains when locking kicks in and how to tune it for your workload.

**Strategies that use locking:**

| Strategy | Locking Required | When |
| -------- | ---------------- | ---- |
| FixedWindowStrategy | Conditional | Only for sub-second windows (< 1s) |
| SlidingWindowLogStrategy | Always | Multi-step log operations |
| SlidingWindowCounterStrategy | Conditional | Only for sub-second windows |
| TokenBucketStrategy | Always | Token refill + consume atomicity |
| TokenBucketWithDebtStrategy | Always | Debt tracking + token operations |
| LeakyBucketStrategy | Always | Queue level management |
| LeakyBucketWithQueueStrategy | Always | Queue operations |
| GCRAStrategy | Always | TAT calculations |

**Sub-second window considerations:**

For windows less than 1 second (e.g., `"100/500ms"`), `FixedWindowStrategy` and `SlidingWindowCounterStrategy` must use explicit locking because:

1. Minimum TTL for backend keys is typically 1 second
2. Window boundaries must be tracked separately from key expiration
3. Multiple operations (read window start, check/reset counter) must be atomic

```python
# This uses locking (sub-second window)
fast_throttle = HTTPThrottle(uid="fast", rate="10/100ms", strategy=FixedWindowStrategy())

# This does NOT use locking (>= 1 second window, uses atomic increment)
normal_throttle = HTTPThrottle(uid="normal", rate="100/s", strategy=FixedWindowStrategy())
```

**Lock contention under high load:**

Under high concurrency with locking strategies, you may experience:

- **Increased latency**: Requests wait for lock acquisition (up to `blocking_timeout`)
- **Lock timeouts**: If `blocking_timeout` is too short, requests may fail to acquire locks
- **Throughput degradation**: Serial lock acquisition limits parallel processing

**Mitigation strategies:**

1. **Use longer windows when possible** - Prefer `"100/s"` over `"100/500ms"` to avoid sub-second locking
2. **Tune blocking_timeout** - Balance between accuracy and latency:

   ```python
   # For high-throughput, fail fast
   strategy = FixedWindowStrategy(
       lock_config=LockConfig(blocking=True, blocking_timeout=0.05)  # 50ms
   )
   
   # For accuracy-critical, wait longer
   strategy = TokenBucketStrategy(
       lock_config=LockConfig(blocking=True, blocking_timeout=1.0)  # 1s
   )
   ```

3. **Consider non-locking strategies** - For >= 1 second windows, `FixedWindowStrategy` uses atomic `increment_with_ttl` without locks
4. **Use `blocking=False` for best-effort** - Accepts potential accuracy loss for lower latency:

   ```python
   strategy = TokenBucketStrategy(
       lock_config=LockConfig(blocking=False)  # Fail immediately if lock unavailable
   )
   ```

**Monitoring recommendation:**

Track lock acquisition times and timeouts in production to identify contention issues before they impact users.

## Error Handling

In production, things go wrong—Redis connections drop, Memcached runs out of memory, networks partition. Traffik's error handling system gives you full control over what happens when the unexpected occurs. Do you fail open and let requests through? Fail closed and protect your services? Fall back to a secondary backend? The choice is yours.

### Error Handler Signature

Custom error handlers receive rich context about what went wrong, letting you make intelligent decisions:

```python
from traffik.throttles import ExceptionInfo
from traffik.types import WaitPeriod

async def error_handler(
    connection: HTTPConnection,
    exc_info: ExceptionInfo,  # Rich exception context
) -> WaitPeriod:  # Return wait time in milliseconds
    """
    exc_info contains:
    - exception: `Exception` - Exception instance
    - connection: `HTTPConnection` - HTTP connection
    - cost: int - Request cost
    - rate: `Rate` - Rate limit configuration
    - context: Mapping[str, Any] - Context passed to throttle
    - backend: `ThrottleBackend` - Backend that failed
    - throttle: `Throttle` - Throttle instance
    """
    # Decision logic
    if isinstance(exc_info["exception"], BackendConnectionError):
        return 5000.0  # Fail closed, 5 second wait
    return 0.0  # Allow request
```

### Built-in Error Handlers

Traffik includes pre-built error handlers for common scenarios. From simple string shortcuts for development to sophisticated handlers that combine retries, circuit breakers, and failover logic.

#### Basic Handlers (String Literals)

For straightforward cases, you can use string literals that cover the most common error handling strategies:

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

#### Retry Handler

Retry transient failures:

```python
from traffik.error_handlers import retry

throttle = HTTPThrottle(
    uid="retry-example",
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

#### Failover Handler (Production)

Combines circuit breaker + retry + fallback:

```python
from traffik.error_handlers import failover, CircuitBreaker
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
    on_error=failover(
        backend=fallback,
        breaker=breaker,
        max_retries=2,
    ),
)
```

**Decision flow:**

```text
Error occurs
    ↓
Circuit open? ──Yes──> Use fallback backend
    ↓ No
Retry primary (max 2) ──Success──> Return
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
| High-availability | `failover` | Best resilience |
| Multi-region | `backend_fallback` | Automatic failover |
| Network issues | `retry` | Handle transient errors |
| Observability | Custom handler | Logging and metrics |

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

While Traffik ships with all the standard rate limiting algorithms, sometimes you need something specific to your domain. Maybe you're implementing a proprietary fairness algorithm, or you need to integrate with an external rate limiting service. Here's how to build your own strategy:

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

Need to store rate limit state in DynamoDB? Cassandra? A custom distributed cache? Traffik's backend interface is designed to be extended. Here's the contract your custom backend needs to fulfill:

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

Good rate limiting code deserves good tests. Traffik is designed to be testable—use the in-memory backend for fast, isolated unit tests, or spin up real Redis/Memcached instances for integration testing. Here are some patterns to get you started.

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

## Benchmarks

Numbers matter when you're adding middleware to every request. We've run extensive benchmarks comparing Traffik against [SlowAPI](https://github.com/laurents/slowapi), one of the most popular rate limiting libraries for FastAPI. The results below should help you understand the performance characteristics of each library under different workloads.

**Test Environment:** Python 3.9, single-process, 3 iterations per scenario averaged across 3 separate benchmark runs. Performance is expected to improve by 5-15% on Python 3.11+ due to CPython optimizations.

> **Note on Burst Scenario:** The burst test sends requests sequentially (one at a time) to measure per-request overhead. This results in higher latency than concurrent scenarios where async I/O batching benefits apply. Real-world API traffic typically resembles the sustained scenario with multiple concurrent clients.

### InMemory Backend (Fixed Window)

| Scenario | Metric | Traffik | SlowAPI | Notes |
| -------- | ------ | ------- | ------- | ----- |
| Low Load (50 req) | Requests/sec | 386 | 367 | Both handle light traffic well |
| | P50 Latency | 2.55ms | 3.78ms | |
| | P95 Latency | 6.03ms | 6.33ms | |
| | P99 Latency | 8.05ms | 8.95ms | |
| High Load (200 req) | Requests/sec | 381 | 367 | Under sustained pressure |
| | P50 Latency | 3.83ms | 4.66ms | |
| | P95 Latency | 7.29ms | 8.09ms | |
| | P99 Latency | 9.19ms | 10.02ms | |
| Sustained (500 req) | Requests/sec | 1,091 | 1,014 | High concurrency batches |
| | P50 Latency | 1.06ms | 1.79ms | Traffik scales better |
| | P95 Latency | 2.96ms | 3.70ms | |
| | P99 Latency | 4.40ms | 5.10ms | |
| Burst (100 req) | Requests/sec | 355 | 433 | Rapid sequential requests |
| | P50 Latency | 5.61ms | 2.53ms | |
| | P95 Latency | 8.68ms | 5.90ms | |
| | P99 Latency | 11.53ms | 9.01ms | |

### Redis Backend (Fixed Window)

| Scenario | Metric | Traffik | SlowAPI | Notes |
| -------- | ------ | ------- | ------- | ----- |
| Low Load (50 req) | Requests/sec | 309 | 321 | Similar performance |
| | P50 Latency | 2.56ms | 2.45ms | |
| | P95 Latency | 5.83ms | 5.41ms | |
| | P99 Latency | 9.71ms | 7.45ms | |
| High Load (200 req) | Requests/sec | 444 | 459 | Comparable throughput |
| | P50 Latency | 2.01ms | 2.01ms | |
| | P95 Latency | 3.26ms | 3.23ms | |
| | P99 Latency | 5.84ms | 4.50ms | |
| Sustained (500 req) | Requests/sec | 978 | 917 | Traffik 7% faster |
| | P50 Latency | 0.90ms | 0.96ms | |
| | P95 Latency | 1.52ms | 1.63ms | |
| | P99 Latency | 2.19ms | 2.31ms | |
| Burst (100 req) | Requests/sec | 352 | 398 | SlowAPI edge |
| | P50 Latency | 2.19ms | 2.29ms | |
| | P95 Latency | 5.09ms | 3.61ms | |
| | P99 Latency | 8.74ms | 5.54ms | |

### Memcached Backend (Fixed Window)

| Scenario | Metric | Traffik | SlowAPI | Notes |
| -------- | ------ | ------- | ------- | ----- |
| Low Load (50 req) | Requests/sec | 369 | 301 | Traffik 23% faster |
| | P50 Latency | 2.22ms | 3.55ms | |
| | P95 Latency | 3.70ms | 6.25ms | |
| | P99 Latency | 5.67ms | 9.24ms | |
| High Load (200 req) | Requests/sec | 474 | 390 | Traffik 22% faster |
| | P50 Latency | 1.89ms | 2.28ms | |
| | P95 Latency | 2.95ms | 5.08ms | |
| | P99 Latency | 4.20ms | 8.49ms | |
| Sustained (500 req) | Requests/sec | 972 | 877 | Traffik 11% faster |
| | P50 Latency | 0.91ms | 0.96ms | |
| | P95 Latency | 1.62ms | 1.79ms | |
| | P99 Latency | 2.40ms | 4.95ms | |
| Burst (100 req) | Requests/sec | 419 | 423 | Comparable |
| | P50 Latency | 2.10ms | 2.07ms | |
| | P95 Latency | 3.59ms | 3.50ms | |
| | P99 Latency | 4.89ms | 6.44ms | |

### Sliding Window Counter Strategy (InMemory)

| Scenario | Metric | Traffik | SlowAPI | Notes |
| -------- | ------ | ------- | ------- | ----- |
| Low Load (50 req) | Requests/sec | 273 | 172 | Traffik 59% faster |
| | P50 Latency | 3.97ms | 5.39ms | |
| | P95 Latency | 8.93ms | 11.80ms | |
| | P99 Latency | 11.27ms | 13.81ms | |
| High Load (200 req) | Requests/sec | 305 | 135 | Traffik 126% faster |
| | P50 Latency | 2.71ms | 7.39ms | |
| | P95 Latency | 6.52ms | 11.18ms | |
| | P99 Latency | 9.30ms | 13.31ms | |
| Sustained (500 req) | Requests/sec | 457 | 420 | Traffik 9% faster |
| | P50 Latency | 1.65ms | 1.98ms | |
| | P95 Latency | 5.03ms | 5.05ms | |
| | P99 Latency | 6.96ms | 6.68ms | |
| Burst (100 req) | Requests/sec | 147 | 190 | SlowAPI edge |
| | P50 Latency | 6.77ms | 4.54ms | |
| | P95 Latency | 11.23ms | 11.15ms | |
| | P99 Latency | 14.94ms | 13.48ms | |

### WebSocket Rate Limiting

Real-time applications need real-time protection. Traffik is one of the few libraries offering native WebSocket rate limiting—limiting not just connection attempts, but individual messages within an open connection. No direct comparison available as SlowAPI does not support WebSocket throttling.

| Scenario | Messages/sec | P50 Latency | P95 Latency | P99 Latency |
| -------- | ------------ | ----------- | ----------- | ----------- |
| Low Load (50 msg) | 4,307 | 0.21ms | 0.47ms | 0.69ms |
| High Load (200 msg) | 5,709 | 0.15ms | 0.63ms | 2.48ms |
| Sustained (500 msg) | 6,660 | 0.15ms | 0.30ms | 0.65ms |
| Burst Traffic | 4,873 | 0.18ms | 0.82ms | 3.43ms |
| Concurrent (10 conn) | 2,899 | 1.87ms | 4.36ms | 6.39ms |

### Key Observations

After running thousands of requests across different backends, strategies, and load patterns, here's what the numbers tell us:

- **InMemory**: Both libraries perform similarly with Traffik showing better scaling under sustained load
- **Redis**: Performance is comparable, with both libraries handling distributed workloads well
- **Memcached**: Traffik shows consistent advantages across scenarios (11-23% faster)
- **Sliding Window**: Traffik's implementation is significantly faster (up to 126% in high load scenarios)
- **Correctness**: Both libraries pass race condition and distributed correctness tests
- **WebSocket**: Sub-millisecond P50 latencies make Traffik suitable for real-time applications

### Running Benchmarks

You can run benchmarks yourself to verify results on your hardware:

```bash
# HTTP comparison with InMemory backend
uv run python benchmarks/comparison.py --scenarios low,high,burst,sustained --iterations 3

# With Redis backend
uv run python benchmarks/comparison.py --traffik-backend redis --slowapi-backend redis \
    --traffik-redis-url redis://localhost:6379/0 --slowapi-redis-url redis://localhost:6379/0

# With Memcached backend  
uv run python benchmarks/comparison.py --traffik-backend memcached --slowapi-backend memcached \
    --traffik-memcached-url memcached://localhost:11211 --slowapi-memcached-url memcached://localhost:11211

# With sliding-window-counter strategy
uv run python benchmarks/comparison.py --traffik-strategy sliding-window-counter \
    --slowapi-strategy sliding-window-counter

# WebSocket benchmarks
uv run python benchmarks/websockets.py --scenarios low,high,burst,sustained,concurrent --iterations 3

# See all options
uv run python benchmarks/comparison.py --help
uv run python benchmarks/websockets.py --help
```

## Performance

Rate limiting runs on every request, so even small inefficiencies add up. This section shares hard-won lessons from production deployments to help you get the most out of Traffik.

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
   async def slow_identifier(conn):
       user = await db.get_user(extract_jwt(conn))  # DB query!
       return f"user:{user.id}"
   ```

6. **Avoid logging in backend operations: **Logging can cause ~10× slowdown**. Especially when the logging backend is slow (e.g., file I/O).

## API Reference

This section provides a comprehensive reference for Traffik's public API. For quick examples, see the sections above. For the full parameter lists and method signatures, read on.

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
    dynamic_backend: bool = False,
    min_wait_period: Optional[int] = None,
    headers: Optional[Mapping[str, str]] = None,
    on_error: Union[Literal["allow", "throttle", "raise"], ErrorHandler] = None,
    cache_ids: bool = True,
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
- `dynamic_backend`: Enable runtime backend resolution (default: False)
- `min_wait_period`: Minimum wait time in milliseconds
- `headers`: Extra headers for throttled responses
- `on_error`: Error handling strategy
- `cache_ids`: Cache computed identifiers in connection state (default: True). Avoids recomputation on multiple calls.

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
    # Constructor parameters (all backends)
    def __init__(
        namespace: str,                          # Key prefix for isolation
        identifier: ConnectionIdentifier = None, # Client identifier function
        handle_throttled: ConnectionThrottledHandler = None,
        persistent: bool = False,                # Persist data across restarts
        on_error: Literal["allow", "throttle", "raise"] = "throttle",
        lock_blocking: bool = None,              # Wait for locks (default: True)
        lock_ttl: float = None,                  # Lock auto-release timeout
        lock_blocking_timeout: float = None,     # Max lock wait time
    )

    # Methods
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
    async def lock(
        name: str,
        ttl: float = None,              # Override backend lock_ttl
        blocking: bool = None,          # Override backend lock_blocking
        blocking_timeout: float = None, # Override backend lock_blocking_timeout
    ) -> AsyncLockContext
    async def reset() -> None
    async def close() -> None
    
# Context manager
async with backend(app, persistent=True, close_on_exit=True):
    ...
```

**Backend-specific parameters:**

| Backend | Extra Parameters |
| ------- | ---------------- |
| `InMemoryBackend` | `number_of_shards=3`, `cleanup_frequency=3.0` |
| `RedisBackend` | `connection` (URL or factory), `lock_type="redis"\|"redlock"` |
| `MemcachedBackend` | `host`, `port`, `pool_size`, `pool_minsize`, `track_keys=False` |

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
- `GCRAStrategy(burst_tolerance_ms: float = 0.0)`

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

### Utilities

```python
from traffik import (
    get_remote_address,
    set_lock_ttl,
    set_lock_blocking,
    set_lock_blocking_timeout,
    get_lock_ttl,
    get_lock_blocking,
    get_lock_blocking_timeout,
    is_throttled,
)

# Get client IP address
ip = get_remote_address(connection)  # Checks X-Forwarded-For, then client.host

# Configure global lock behavior
set_lock_ttl(5.0)         # Auto-release locks after 5 seconds
set_lock_blocking(True)   # Enable blocking locks
set_lock_blocking_timeout(2.0)    # Max 2s wait for locks

# Read current configuration
ttl = get_lock_ttl()      # float | None
blocking = get_lock_blocking()    # bool
timeout = get_lock_blocking_timeout()     # float | None

# Check if connection was throttled
if is_throttled(websocket):
    # Handle throttled connection
    pass
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
    retry,
    failover,
    CircuitBreaker,
)

# String literal handlers (built-in)
on_error="allow"      # Allow all requests on errors
on_error="throttle"   # Throttle all requests on errors (min_wait_period or 1000ms wait if set)
on_error="raise"      # Re-raise exceptions
```

## Contributing

Want to help make Traffik better? We'd love to have you! Whether it's fixing bugs, adding features, improving documentation, or just reporting issues, every contribution matters. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT License - see [LICENSE](LICENSE) file.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Acknowledgments

This project used AI assistance (GitHub Copilot) for writing documentation and test generation. All AI-generated content was reviewed and vetted by me.
