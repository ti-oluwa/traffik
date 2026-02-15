# Response Headers

Rate limiting is half the battle. The other half is telling clients *how* they're doing before they slam into a wall.

Without response headers, your clients are flying blind - they have no idea how many requests they have left, when their window resets, or why they suddenly got a `429`. With the right headers, a well-behaved client can slow itself down gracefully, show users a meaningful "try again in X seconds" message, and stop hammering your API unnecessarily.

This page covers the `Header` and `Headers` system that Traffik uses to **resolve** rate limit header values. Traffik does not automatically inject headers into responses — it computes the values and returns them. It's up to you to attach them to your response however you see fit (e.g., via middleware, a custom dependency, or a Starlette response). The `headers=` parameter on a throttle tells Traffik *which* headers to resolve, not which ones to auto-inject.

---

## Why Headers Matter

Standard rate limit headers have become a de facto convention - GitHub, Stripe, Cloudflare, and most large APIs all use some variation of these three:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Maximum requests allowed in the current window |
| `X-RateLimit-Remaining` | Requests left in the current window |
| `Retry-After` | Seconds to wait before trying again (RFC 6585) |

A client that reads these headers can back off gracefully, show users a countdown timer, or queue requests intelligently. A client that doesn't get them just crashes and retries until you block it.

Traffik lets you attach these headers to every response, or only to throttled responses - whichever fits your API contract.

---

## Built-in Header Presets

For most use cases, you don't need to build your own headers from scratch. Traffik ships two ready-made presets, available from both `traffik` and `traffik.headers`:

```python
from traffik.headers import DEFAULT_HEADERS_ALWAYS, DEFAULT_HEADERS_THROTTLED
# or equivalently:
# from traffik import DEFAULT_HEADERS_ALWAYS, DEFAULT_HEADERS_THROTTLED
```

**`DEFAULT_HEADERS_ALWAYS`** - Sends `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After` on *every* response. Clients can always see their current status.

**`DEFAULT_HEADERS_THROTTLED`** - Sends the same three headers *only* when a client is throttled (i.e., receives a `429`). Quieter by default; headers appear only when they matter most.

!!! tip "Which should I use?"
    `DEFAULT_HEADERS_ALWAYS` is friendlier for client developers - they can poll their remaining quota without making a special endpoint for it. `DEFAULT_HEADERS_THROTTLED` keeps response size smaller on normal requests and is common in APIs where clients are expected to track their own usage. GitHub uses "always"; many simpler APIs use "throttled".

---

## Attaching Headers to a Throttle

Pass a `headers` argument when constructing any throttle:

```python
from traffik import HTTPThrottle
from traffik.headers import DEFAULT_HEADERS_ALWAYS

throttle = HTTPThrottle(
    "api:items",
    rate="100/min",
    headers=DEFAULT_HEADERS_ALWAYS,
)
```

The `headers=` parameter tells Traffik which headers to *resolve* on a hit. On every hit (throttled or not, depending on the preset), the header values are computed from the current strategy statistics and returned by `throttle.get_headers()`. You are responsible for attaching them to your response — for example, by calling `response.headers.update(resolved)` in a custom handler or middleware.

---

## The `Header` Class

Under the hood, each header in a `Headers` collection is a `Header` instance - a small, immutable object that knows *when* to include itself and *how* to compute its value.

```python
from traffik.headers import Header
```

### Built-in Header Builders

`Header` exposes four class methods for the standard rate limit headers:

```python
Header.LIMIT(when=...)          # X-RateLimit-Limit: max hits per window
Header.REMAINING(when=...)      # X-RateLimit-Remaining: hits left this window
Header.RESET_SECONDS(when=...)  # Retry-After: seconds until window resets
Header.RESET_MILLISECONDS(when=...)  # X-RateLimit-Reset-Ms: milliseconds until reset
```

Each returns a `Header` instance that resolves its value at request time from the live strategy statistics. The `when` parameter is **required** - it controls when the header appears in the response.

!!! note "Why is `when` required on class methods?"
    Traffik makes the inclusion condition explicit rather than defaulting silently. Forgetting a `when` value on a header builder would be a common footgun - should a `Retry-After` header appear on every `200` response? Probably not. Making it explicit keeps the contract clear.

---

## Header Conditions: `when="always"` vs `when="throttled"`

The `when` parameter on every `Header` controls its inclusion logic:

```python
# Include this header on every response, throttled or not
Header.REMAINING(when="always")

# Include this header only when the client is throttled (hits_remaining <= 0)
Header.REMAINING(when="throttled")
```

You can also pass a **custom callable** for more nuanced conditions:

```python
def warn_when_low(connection, stat, context):
    """Include the header when the client is below 10% quota remaining."""
    return stat.hits_remaining < (stat.rate.limit * 0.1)

Header.REMAINING(when=warn_when_low)
```

The callable receives three arguments:

- `connection` - the current `Request` or `WebSocket`
- `stat` - the `StrategyStat` object with `.hits_remaining`, `.rate`, `.wait_ms`, etc.
- `context` - the throttle context dict, or `None`

It should return `True` to include the header, `False` to skip it.

### Chaining with `.when()`

If you have an existing `Header` instance and want a copy with a different condition, use the `.when()` method or the convenience properties:

```python
base = Header.REMAINING(when="always")

# These are all equivalent
throttled_only = base.when("throttled")
throttled_only = base.throttled

always_on = base.when("always")
always_on = base.always
```

---

## Custom Header Values

### Static values

For a header whose value never changes, just pass a string:

```python
from traffik.headers import Header

# A static policy string (useful for RFC 9440 RateLimit-Policy header)
policy_header = Header("100;w=60")
```

### Dynamic values via resolver function

For a value computed at request time, pass a callable with signature
`(connection, stat, context) -> str`:

```python
from traffik.headers import Header

def my_resolver(connection, stat, context):
    if stat:
        return str(int(stat.hits_remaining))
    return "unknown"

custom_dynamic = Header(my_resolver, when="always")
```

The resolver is called on every request where the `when` condition passes. Keep it fast - it's on the hot path.

---

## Building a `Headers` Collection

`Headers` is a mapping of header name strings to `Header` instances (or raw strings for static values). It's the recommended way to package headers for a throttle:

```python
from traffik import HTTPThrottle
from traffik.headers import Headers, Header

my_headers = Headers({
    "X-RateLimit-Limit": Header.LIMIT(when="always"),
    "X-RateLimit-Remaining": Header.REMAINING(when="always"),
    "Retry-After": Header.RESET_SECONDS(when="throttled"),
    "RateLimit-Policy": Header("100;w=60"),  # (1)!
})

throttle = HTTPThrottle("api:items", rate="100/min", headers=my_headers)
```

1. A static string is perfectly valid here - `Headers` will recognize it as always-included and optimise resolution accordingly.

!!! tip "Performance note"
    `Headers` is smart about optimisation. If all headers in a collection are static strings with `when="always"`, Traffik pre-encodes them once and skips the resolution step entirely on every request. The more static headers you have, the cheaper header resolution gets.

---

## Merging Headers

`Headers` supports the `|` operator for creating merged copies, and `|=` for in-place merging. This is useful when you want to extend a preset with extra headers:

```python
from traffik.headers import Headers, Header, DEFAULT_HEADERS_ALWAYS

extra = Headers({
    "X-Service-Name": Header("my-api"),
    "X-RateLimit-Reset-Ms": Header.RESET_MILLISECONDS(when="always"),
})

# Create a merged copy - DEFAULT_HEADERS_ALWAYS is not mutated
combined = DEFAULT_HEADERS_ALWAYS | extra
```

You can also `.copy()` a `Headers` instance for a shallow duplicate:

```python
from traffik.headers import DEFAULT_HEADERS_ALWAYS, Header

my_copy = DEFAULT_HEADERS_ALWAYS.copy()
my_copy["X-Service-Name"] = Header("my-api")
```

---

## Disabling a Header Per-Request

Sometimes you want to suppress a specific header for a particular call - say, you have a global `DEFAULT_HEADERS_ALWAYS` on a throttle but want to hide `X-RateLimit-Remaining` on one specific endpoint.

Use `Header.DISABLE` as a sentinel value in an override mapping passed to `throttle.get_headers()`:

```python
from traffik.headers import Header

# Resolve headers, but skip X-RateLimit-Remaining for this call
resolved = await throttle.get_headers(
    connection,
    headers={"X-RateLimit-Remaining": Header.DISABLE},
)
```

`Header.DISABLE` is a string sentinel (`":___disabled___:"`). The resolver checks identity (`is`), not equality - so a plain string with the same content would not trigger the disable logic.

!!! warning "Identity matters"
    Always use `Header.DISABLE` directly. A variable holding the same string value won't work because Python string interning doesn't guarantee identity for all strings.

---

## Real-World Example: GitHub/Stripe-Style Headers

Here's a complete setup that matches the header pattern used by most large REST APIs - headers resolved on every hit, plus a millisecond-precision reset time for clients that need it:

```python
from fastapi import FastAPI, Depends, Request, Response
from traffik import HTTPThrottle
from traffik.backends.inmemory import InMemoryBackend
from traffik.headers import Headers, Header

# --- Build a custom header collection ---
api_headers = Headers({
    # Core rate limit headers - always present
    "X-RateLimit-Limit": Header.LIMIT(when="always"),
    "X-RateLimit-Remaining": Header.REMAINING(when="always"),

    # Retry-After only on 429 (RFC 6585 compliance)
    "Retry-After": Header.RESET_SECONDS(when="throttled"),

    # Millisecond-precision reset for clients that want precision  (1)!
    "X-RateLimit-Reset-Ms": Header.RESET_MILLISECONDS(when="always"),

    # A static policy descriptor (RFC 9440)
    "RateLimit-Policy": Header("1000;w=60"),
})

# --- Wire everything up ---
backend = InMemoryBackend(namespace="myapi")
app = FastAPI(lifespan=backend.lifespan)

api_throttle = HTTPThrottle(
    "api:v1",
    rate="1000/min",
    headers=api_headers,
)

@app.get("/items")
async def list_items(
    request: Request,
    response: Response,  # (2)!
    _=Depends(api_throttle),
):
    # Resolve headers for the current request and attach them to the response.
    # get_headers() fetches live stats from the backend automatically.
    headers = await api_throttle.get_headers(request)  # (3)!
    response.headers.update(headers)
    return {"items": ["widget", "gizmo"]}
```

1. Some clients (JavaScript frontends, mobile apps) find millisecond precision more useful for scheduling retries than whole seconds.
2. FastAPI injects a mutable `Response` object. Headers set on it are merged into the final response automatically.
3. `get_headers()` calls `throttle.stat()` internally to fetch the current rate limit statistics from the backend, then resolves each header's value. You don't need to pass `stat` manually.

!!! tip "Using `JSONResponse` directly"
    If you prefer to build the response yourself, pass `headers=` to `JSONResponse`:
    ```python
    from fastapi.responses import JSONResponse
    @app.get("/items")
    async def list_items(request: Request, _=Depends(api_throttle)):
        headers = await api_throttle.get_headers(request)
        return JSONResponse({"items": ["widget", "gizmo"]}, headers=headers)
    ```

!!! note "Headers on throttled responses"
    When a client is throttled (HTTP 429), the throttle raises an exception before your handler runs. To attach headers to 429 responses, add them in a custom exception handler. See [Custom Throttled Handlers](throttled-handlers.md) for the pattern.

A typical successful response from this setup would look like:

```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 997
X-RateLimit-Reset-Ms: 43200
RateLimit-Policy: 1000;w=60
Content-Type: application/json

{"items": ["widget", "gizmo"]}
```

And a throttled response:

```http
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 0
X-RateLimit-Reset-Ms: 8400
RateLimit-Policy: 1000;w=60
Retry-After: 9
Content-Type: application/json

{"detail": "Too many requests. Retry in 9 seconds."}
```

!!! tip "RFC 9440 - The emerging standard"
    The IETF's [RFC 9440](https://www.rfc-editor.org/rfc/rfc9440) standardises rate limit headers. If your API needs to be strictly spec-compliant, the `RateLimit` (combined) and `RateLimit-Policy` headers are the future direction. The `X-RateLimit-*` prefix is widely understood but not standardised - it's the safe pragmatic choice for compatibility today.

---

## Summary

| Concept | What it does |
|---|---|
| `DEFAULT_HEADERS_ALWAYS` (from `traffik.headers`) | Preset: `Limit` + `Remaining` + `Retry-After` resolved on every hit |
| `DEFAULT_HEADERS_THROTTLED` (from `traffik.headers`) | Preset: same three headers, but only resolved on throttled (`429`) hits |
| `Header.LIMIT(when=...)` | Resolves to the window's max hit count |
| `Header.REMAINING(when=...)` | Resolves to hits left this window |
| `Header.RESET_SECONDS(when=...)` | Resolves to seconds until window resets |
| `Header.RESET_MILLISECONDS(when=...)` | Resolves to milliseconds until window resets |
| `Header("static")` | A fixed string, always included |
| `Header(fn, when=...)` | A dynamic value via resolver callable |
| `Headers({...})` | A collection of headers attached to a throttle |
| `headers_a \| headers_b` | Merge two header collections (non-mutating) |
| `Header.DISABLE` | Sentinel to suppress a specific header in a call |
