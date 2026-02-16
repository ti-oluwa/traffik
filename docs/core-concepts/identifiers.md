# Identifiers

An identifier is **how Traffik knows who is who**. Before any strategy runs, Traffik
calls the identifier function to get a string key that represents the caller. All
counter increments, lock names, and backend keys are namespaced under that key.

Get the identifier right and you get per-user, per-tenant, or per-API-key rate
limiting. Return the wrong thing and everyone shares a single counter.

---

## Default identifier: IP address

If you do not provide an identifier, Traffik uses `get_remote_address()`, which reads
(in order):

1. The first value in the `X-Forwarded-For` header
2. The `remote-addr` header
3. `connection.client.host`

The fallback when none of those are available is `ANONYMOUS_IDENTIFIER` (`"__anonymous__"`).

```python
from traffik import HTTPThrottle

# No identifier → defaults to IP address
throttle = HTTPThrottle(uid="my-api", rate="100/min")
```

---

## Custom identifiers

An identifier is any `async` function that takes an `HTTPConnection` and returns
something that can be converted to a string:

```python
async def my_identifier(connection: HTTPConnection) -> str:
    ...
```

Pass it to the throttle:

```python
from traffik import HTTPThrottle

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    identifier=my_identifier,
)
```

You can also set a default identifier at the backend level, which applies to all
throttles that use that backend and do not specify their own:

```python
from traffik.backends.redis import RedisBackend

backend = RedisBackend(
    "redis://localhost:6379",
    namespace="myapp",
    identifier=my_identifier,  # backend-level default
)
```

---

## Identifier examples

=== "IP-based (explicit)"

    ```python
    from starlette.requests import HTTPConnection
    from traffik.utils import get_remote_address
    from traffik.config import ANONYMOUS_IDENTIFIER

    async def ip_identifier(connection: HTTPConnection) -> str:
        return get_remote_address(connection) or ANONYMOUS_IDENTIFIER
    ```

=== "User ID from JWT"

    ```python
    from starlette.requests import Request

    async def user_identifier(connection: Request) -> str:
        user = connection.state.user  # populated by your auth middleware
        if user is None:
            return "anonymous"
        return f"user:{user.id}"
    ```

=== "API key from header"

    ```python
    from starlette.requests import HTTPConnection

    async def api_key_identifier(connection: HTTPConnection) -> str:
        api_key = connection.headers.get("X-API-Key")
        if not api_key:
            return "no-key"
        return f"apikey:{api_key}"
    ```

=== "Tenant ID from subdomain"

    ```python
    from starlette.requests import HTTPConnection

    async def tenant_identifier(connection: HTTPConnection) -> str:
        host = connection.headers.get("host", "")
        tenant = host.split(".")[0]  # e.g. "acme" from "acme.example.com"
        return f"tenant:{tenant}"
    ```

---

## `EXEMPTED`: bypassing all throttle logic

Return the `EXEMPTED` sentinel to completely bypass throttling for a connection.
Traffik will not call the strategy, will not touch the backend, and will not
increment any counter. The overhead is essentially zero.

```python
from starlette.requests import HTTPConnection
from traffik.types import EXEMPTED

async def identifier_with_allowlist(connection: HTTPConnection):
    ip = connection.headers.get("x-forwarded-for", "").split(",")[0].strip()

    # Internal health checkers, CI runners, etc.
    if ip in {"10.0.0.1", "10.0.0.2"}:
        return EXEMPTED  # this connection is completely untouched

    return ip
```

!!! tip "EXEMPTED is the right tool for internal traffic"
    Allowlisting via `EXEMPTED` costs nothing. Returning an identifier that you
    then map to an unlimited `Rate` still touches the backend and runs the strategy.
    When you want zero overhead, return `EXEMPTED`.

---

## `ANONYMOUS_IDENTIFIER`: the fallback constant

When `get_remote_address()` cannot determine an IP address (e.g., a Unix socket
connection, a test client without a client address), it returns `None`. The default
identifier maps that `None` to the `ANONYMOUS_IDENTIFIER` constant, which is the
string `"__anonymous__"`.

All anonymous connections share a single counter, so be aware that a flooded
anonymous connection will consume the rate limit for all other anonymous connections.
In most real deployments this is not an issue because the IP header is always
present.

```python
from traffik.config import ANONYMOUS_IDENTIFIER

# ANONYMOUS_IDENTIFIER == "__anonymous__"
```

---

## Identifier caching with `cache_ids`

By default (`cache_ids=True`), Traffik calls your identifier function **once per
request** and caches the result in the request's context for the lifetime of that
request. If multiple throttles apply to the same endpoint, they all reuse the cached
value and your identifier function is never called more than once.

```python
from traffik import HTTPThrottle

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    identifier=my_identifier,
    cache_ids=True,   # default — identifier called once, result reused
)
```

Set `cache_ids=False` only if your identifier function's result can legitimately
change between throttle invocations within the same request (which is almost never
the case).

!!! warning "Avoid slow I/O in identifier functions on hot paths"
    Your identifier runs on **every request**. An identifier that makes a database
    query or an external HTTP call to resolve a user ID will add that latency to
    every single request that hits any throttle. Consider caching resolved identities
    in `request.state` yourself, or keep the identifier to headers and path
    parameters only.

    If you must do I/O, at least make sure `cache_ids=True` (the default) so the
    I/O only happens once per request, not once per throttle per request.


