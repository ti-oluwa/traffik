# API Reference

Quick reference for every public class and function in Traffik. Each entry lists the import path, constructor parameters, and key methods. Follow the guide links for full usage examples.

---

## Throttles

### `HTTPThrottle`

Rate limiter for HTTP requests. The most common entry point. Can also be accessed as `RequestThrottle`

```python
from traffik import HTTPThrottle
```

**Guide:** [Dependencies](integration/dependencies.md) · [Decorators](integration/decorators.md) · [Direct Usage](integration/direct-usage.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uid` | `str` | required | Unique identifier for this throttle. Used as the registry key and storage namespace. |
| `rate` | `str \| Rate \| async callable` | required | Rate limit. A string like `"100/min"`, a `Rate` object, or an async callable `(request, context) -> Rate`. |
| `cost` | `int \| async callable` | `1` | Quota consumed per hit. An integer or an async callable `(request, context) -> int`. |
| `backend` | `ThrottleBackend` | `None` | Storage backend. Falls back to the backend set on the active context if not specified. |
| `identifier` | `async callable` | `None` | Async callable `(request) -> str` that returns the throttle key. Defaults to the backend's identifier. Return `EXEMPTED` to skip throttling. |
| `strategy` | `ThrottleStrategy` | `None` | Throttling algorithm. Defaults to the backend's default strategy. |
| `handle_throttled` | `async callable` | `None` | Custom handler called when the client is throttled. Receives `(request, wait_ms, throttle, context)`. |
| `headers` | `Headers \| dict` | `None` | Response headers to resolve on each hit. See [Response Headers](advanced/headers.md). |
| `on_error` | `"allow" \| "throttle" \| "raise" \| callable` | `None` | Behaviour when the backend raises. `"allow"` passes the request through; `"throttle"` rejects it; `"raise"` re-raises. |
| `registry` | `ThrottleRegistry` | `GLOBAL_REGISTRY` | Registry to register this throttle in. |
| `rules` | `Iterable[ThrottleRule]` | `None` | Gating rules. All must pass for the throttle to fire. |
| `context` | `dict` | `None` | Static context dict passed to rate/cost callables and handlers. |
| `dynamic_backend` | `bool` | `False` | Resolve the backend from request context on each hit instead of using a fixed instance. |
| `min_wait_period` | `int` | `None` | Minimum wait floor in milliseconds for throttled responses. |
| `cache_ids` | `bool` | `True` | Cache connection identifiers within a single request for performance. |
| `dynamic_rules` | `bool` | `False` | Re-evaluate registry rules on every hit instead of caching them at startup. |
| `use_method` | `bool` | `True` | If set to False, the throttle will ignore the HTTP method and only use the path for throttling. |

**Key methods:**

| Method | Description |
|---|---|
| `await hit(request, cost=None, context=None)` | Check and consume quota. Returns the request on success; raises `ConnectionThrottled` when over limit. |
| `await check(request, cost=None, context=None)` | Check quota without consuming it. Returns `True` if the request would pass. |
| `await stat(request, cost=None, context=None)` | Return a `StrategyStat` snapshot for the current connection. |
| `await get_headers(request, headers=None)` | Resolve rate limit headers for the current connection. |
| `await disable()` | Disable this throttle. Subsequent `hit()` calls return immediately. |
| `await enable()` | Re-enable a disabled throttle. |
| `is_disabled` | `True` if the throttle is currently disabled. |
| `await update_rate(rate)` | Swap the rate limit atomically. |
| `await update_backend(backend)` | Swap the backend atomically. |
| `await update_strategy(strategy)` | Swap the strategy atomically. |
| `await update_cost(cost)` | Update the cost atomically. |
| `await update_identifier(fn)` | Swap the identifier function atomically. |
| `await update_headers(headers)` | Replace the header collection atomically. |
| `await update_min_wait_period(ms)` | Update the minimum wait floor atomically. |
| `await update_handle_throttled(handler)` | Swap the throttled-response handler atomically. |

---

### `WebSocketThrottle`

Rate limiter for WebSocket connections.

```python
from traffik import WebSocketThrottle
```

**Guide:** [Dependencies](integration/dependencies.md#websocket-routes) · [Direct Usage](integration/direct-usage.md)

Takes mostly the same parameters as `HTTPThrottle` but with a few exceptions, and scoped to `WebSocket` connections. Use inside a WebSocket handler to throttle per-message.

---

### `MiddlewareThrottle`

Throttle applied at the ASGI middleware layer before route handlers execute.

```python
from traffik.throttles import MiddlewareThrottle
```

**Guide:** [Middleware](integration/middleware.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `throttle` | `HTTPThrottle` | required | The underlying `HTTPThrottle` to wrap. |
| `cost` | `int \| async callable \| None` | `None` | Override cost for middleware context. `None` uses the wrapped throttle's cost. |
| `predicate` | `async callable \| None` | `None` | Async callable `(request) -> bool`. If provided, the throttle only applies when it returns `True`. |

---

## Backends

### `InMemoryBackend`

In-process storage backend. No external dependencies. Not shared across processes.

```python
from traffik.backends.inmemory import InMemoryBackend
```

**Guide:** [Backends](core-concepts/backends.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `namespace` | `str` | `"inmemory"` | Key prefix for storage isolation. |
| `persistent` | `bool` | `False` | Keep data in memory across `lifespan` restarts within the same process. |
| `on_error` | `"allow" \| "throttle" \| "raise" \| callable` | `"throttle"` | Default error handling for throttles using this backend. |
| `identifier` | `async callable` | `None` | Default identifier for throttles that don't specify one. |
| `handle_throttled` | `async callable` | `None` | Default throttled handler for throttles that don't specify one. |
| `number_of_shards` | `int` | `3` | Number of internal storage shards. Higher values reduce lock contention under concurrent load. |
| `cleanup_frequency` | `float \| None` | `10.0` | Seconds between expired-key cleanup sweeps. `None` disables automatic cleanup. |
| `lock_kind` | `"fair" \| "unfair"` | `"unfair"` | Lock fairness. `"fair"` gives waiting coroutines FIFO ordering; `"unfair"` is faster. |
| `lock_blocking` | `bool \| None` | `None` | Override default lock blocking. Falls back to `TRAFFIK_DEFAULT_BLOCKING` env var. |
| `lock_ttl` | `float \| None` | `None` | Override default lock TTL in seconds. |
| `lock_blocking_timeout` | `float \| None` | `None` | Override default lock timeout. |

**Integration with FastAPI:**

```python
backend = InMemoryBackend()
app = FastAPI(lifespan=backend.lifespan)
```

---

### `RedisBackend`

Distributed backend backed by Redis. Shares counters across all app instances.

```python
from traffik.backends.redis import RedisBackend
```

**Guide:** [Backends](core-concepts/backends.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | required | Redis connection URL, e.g. `"redis://localhost:6379/0"`. |
| `namespace` | `str` | `"redis"` | Key prefix for storage isolation. |
| `persistent` | `bool` | `False` | Keep data across restarts. |
| `on_error` | `"allow" \| "throttle" \| "raise" \| callable` | `"throttle"` | Default error handling. |
| `identifier` | `async callable` | `None` | Default identifier function. |
| `handle_throttled` | `async callable` | `None` | Default throttled handler. |
| `lock_blocking` | `bool \| None` | `None` | Lock blocking setting. |
| `lock_ttl` | `float \| None` | `None` | Lock TTL in seconds. |
| `lock_blocking_timeout` | `float \| None` | `None` | Lock timeout in seconds. |

---

### `MemcachedBackend`

Distributed backend backed by Memcached.

```python
from traffik.backends.memcached import MemcachedBackend
```

**Guide:** [Backends](core-concepts/backends.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `servers` | `list[tuple[str, int]]` | required | List of `(host, port)` tuples. |
| `namespace` | `str` | `"memcached"` | Key prefix for storage isolation. |
| `persistent` | `bool` | `False` | Keep data across restarts. |
| `on_error` | `"allow" \| "throttle" \| "raise" \| callable` | `"throttle"` | Default error handling. |

---

## Rate

### `Rate`

Immutable rate limit definition. Holds the request limit and window duration.

```python
from traffik import Rate
# or
from traffik.rates import Rate
```

**Guide:** [Rate Format](core-concepts/rates.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | `int` | `0` | Maximum requests allowed in the window. `0` with no duration means unlimited. |
| `milliseconds` | `int` | `0` | Window duration in milliseconds. |
| `seconds` | `int` | `0` | Window duration in seconds. |
| `minutes` | `int` | `0` | Window duration in minutes. |
| `hours` | `int` | `0` | Window duration in hours. |

**Key attributes:** `limit`, `expire` (total ms), `rps`, `rpm`, `rph`, `rpd`, `is_subsecond`, `unlimited`

**Parse a rate string:**

```python
rate = Rate.parse("100/min")   # 100 per minute
rate = Rate.parse("10/5s")     # 10 per 5 seconds
rate = Rate.parse("5 per hour")
```

Supported units: `ms`, `s` / `sec` / `second`, `m` / `min` / `minute`, `h` / `hr` / `hour`, `d` / `day`.

---

## Registry

### `ThrottleRegistry`

Manages throttle registration, rule attachment, and group disable/enable.

```python
from traffik.registry import ThrottleRegistry
```

**Guide:** [Throttle Registry](advanced/registry.md)

Throttles register themselves automatically on construction. You rarely need to create a registry manually — the `GLOBAL_REGISTRY` is used by default.

| Method | Description |
|---|---|
| `exist(uid)` | Check if a UID is registered. |
| `add_rules(uid, *rules)` | Attach gating rules to a throttle. Raises `ConfigurationError` if UID not registered. |
| `get_rules(uid)` | Return all rules attached to a throttle. |
| `get_throttle(uid)` | Return the live throttle instance, or `None` if garbage-collected. |
| `await disable(uid)` | Disable the throttle. Returns `True` if found. |
| `await enable(uid)` | Re-enable the throttle. Returns `True` if found. |
| `await disable_all()` | Disable every live throttle in this registry. |
| `await enable_all()` | Re-enable every live throttle in this registry. |
| `clear()` | Unregister everything and wipe all rules and refs. |

---

### `GLOBAL_REGISTRY`

The default `ThrottleRegistry` used when no registry is specified.

```python
from traffik.registry import GLOBAL_REGISTRY
```

---

## Rules

### `ThrottleRule`

A gating rule: the throttle only fires when all attached rules pass.

```python
from traffik.registry import ThrottleRule
```

**Guide:** [Throttle Rules & Wildcards](advanced/rules.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | `str \| Pattern \| None` | `None` | Path pattern to match. Supports `*` (single segment) and `**` (any segments) glob syntax, or a compiled regex. `None` matches all paths. |
| `methods` | `Iterable[str] \| None` | `None` | HTTP methods to match (e.g. `{"POST", "PUT"}`). `None` matches all methods. |
| `predicate` | `async callable \| None` | `None` | Async callable `(connection, [context]) -> bool`. Throttle fires when it returns `True`. |

All specified conditions are combined with AND: path AND method AND predicate must all match.

---

### `BypassThrottleRule`

Skips throttling when the rule matches (inverse logic compared to `ThrottleRule`).

```python
from traffik.registry import BypassThrottleRule
```

**Guide:** [Throttle Rules & Wildcards](advanced/rules.md)

Same parameters as `ThrottleRule`. When all conditions match, the throttle is bypassed for that request.

---

## Headers

### `Header`

A single rate limit response header with a name, value resolver, and inclusion condition.

```python
from traffik.headers import Header
```

**Guide:** [Response Headers](advanced/headers.md)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `v` | `str \| callable` | required | Static string value or a resolver `(connection, stat, context) -> str`. |
| `when` | `"always" \| "throttled" \| callable` | `"throttled"` | Inclusion condition. `"always"` includes on every response. `"throttled"` only on 429s. A callable `(connection, stat, context) -> bool` for custom logic. |

**Built-in builders** (all require `when=`):

| Builder | Header value |
|---|---|
| `Header.LIMIT(when=...)` | `X-RateLimit-Limit`: rate window limit |
| `Header.REMAINING(when=...)` | `X-RateLimit-Remaining`: hits left this window |
| `Header.RESET_SECONDS(when=...)` | `Retry-After`: seconds until window resets |
| `Header.RESET_MILLISECONDS(when=...)` | `X-RateLimit-Reset-Ms`: milliseconds until reset |

**Sentinels:**

- `Header.DISABLE` — Pass as a value in `get_headers()` overrides to suppress a specific header for that call.

---

### `Headers`

A collection of rate limit response headers resolved per request.

```python
from traffik.headers import Headers
```

```python
from traffik.headers import Headers, Header

my_headers = Headers({
    "X-RateLimit-Limit": Header.LIMIT(when="always"),
    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
    "Retry-After": Header.RESET_SECONDS(when="throttled"),
})
```

Supports `|` for non-mutating merge and `.copy()` for shallow duplication.

---

### `DEFAULT_HEADERS_ALWAYS`

Preset `Headers` collection: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After` resolved on every response.

```python
from traffik.headers import DEFAULT_HEADERS_ALWAYS
```

---

### `DEFAULT_HEADERS_THROTTLED`

Preset `Headers` collection: same three headers resolved only on throttled (`429`) responses.

```python
from traffik.headers import DEFAULT_HEADERS_THROTTLED
```

---

## Types and Sentinels

### `EXEMPTED`

Sentinel returned from an identifier function to skip throttling for a connection entirely. No quota is consumed and no backend call is made.

```python
from traffik import EXEMPTED
```

```python
async def my_identifier(request):
    if request.headers.get("x-admin-key") == ADMIN_KEY:
        return EXEMPTED  # skip throttling
    return request.client.host
```

---

### `StrategyStat`

Immutable statistics snapshot returned by `throttle.stat()`.

```python
from traffik.types import StrategyStat
```

| Attribute | Type | Description |
|---|---|---|
| `key` | `Stringable` | The throttle key for this connection. |
| `rate` | `Rate` | The active rate limit. |
| `hits_remaining` | `float` | Hits left in the current window. |
| `wait_ms` | `float` | Milliseconds until the window resets (or until a slot is available). |
| `metadata` | `Mapping \| None` | Additional strategy-specific data. |

---

## Utilities

### `get_remote_address`

Default connection identifier: returns the client IP address as a string.

```python
from traffik import get_remote_address
```

```python
throttle = HTTPThrottle("api:v1", rate="100/min", identifier=get_remote_address)
```

---

## Configuration

Global lock defaults, settable via environment variables or these functions. Throttle-level `lock_blocking`, `lock_ttl`, and `lock_blocking_timeout` parameters override these globals.

```python
from traffik.config import (
    get_lock_ttl, set_lock_ttl,
    get_lock_blocking, set_lock_blocking,
    get_lock_blocking_timeout, set_lock_blocking_timeout,
)
```

| Function | Env var | Default | Description |
|---|---|---|---|
| `get_lock_ttl()` / `set_lock_ttl(v)` | `TRAFFIK_DEFAULT_LOCK_TTL` | `None` | Lock TTL in seconds. `None` means no timeout. |
| `get_lock_blocking()` / `set_lock_blocking(v)` | `TRAFFIK_DEFAULT_BLOCKING` | `True` | Whether to block when acquiring a lock. |
| `get_lock_blocking_timeout()` / `set_lock_blocking_timeout(v)` | `TRAFFIK_DEFAULT_BLOCKING_TIMEOUT` | `None` | Max seconds to wait for a lock. `None` means wait indefinitely. |


