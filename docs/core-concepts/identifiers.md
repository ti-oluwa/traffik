# Identifiers

An identifier is **how Traffik knows who is who**. Before any strategy runs, Traffik
calls the identifier function to get a string key that represents the caller. All
counter increments, lock names, and backend keys are namespaced under that key.

Get the identifier right and you get correct per-user, per-tenant, or per-API-key rate
limiting. Returning the wrong thing could mean everyone shares a single counter.

---

## Default identifier: IP address

If you do not provide an identifier, Traffik uses `get_remote_address()`.

By default, this returns the real socket peer address - `connection.client.host`,
and nothing else. **No proxy header is trusted unless you explicitly tell Traffik
which proxies to trust.**

```python
from traffik import HTTPThrottle

# No identifier -> defaults to IP address (the real socket peer, by default)
throttle = HTTPThrottle(uid="my-api", rate="100/min")
```

The fallback when no client address is available at all (e.g. a Unix socket
connection, or a test client without a client address) is `ANONYMOUS_IDENTIFIER`
(`"__anonymous__"`) - see [below](#anonymous_identifier-the-fallback-constant).

!!! warning "Why proxy headers aren't trusted by default"
    `X-Forwarded-For` and similar headers are just HTTP headers - anyone making a
    direct request can set them to whatever they want. If Traffik read them
    unconditionally, any client could pick its own rate-limit bucket on every
    request just by sending a fake header, defeating IP-based limiting entirely.
    Headers are only ever consulted for a connection whose *immediate* peer is
    an address you've explicitly told Traffik to trust - see the next section.

---

## Behind a reverse proxy: `trusted_proxies` and `proxy_headers`

If your app sits behind a reverse proxy, load balancer, or CDN, the real client's
IP arrives in a header, not as the actual TCP peer address. To have
`get_remote_address` read it, tell it which peer(s) are your own infrastructure
via `trusted_proxies`, and which header convention to read via `proxy_headers`:

```python
import functools
import ipaddress

from traffik import HTTPThrottle, ProxyHeaders, get_remote_address

identifier = functools.partial(
    get_remote_address,
    trusted_proxies=(ipaddress.ip_address("10.0.0.1"),),  # your nginx/Traefik/LB
    proxy_headers=ProxyHeaders.X_FORWARDED_FOR,
)

throttle = HTTPThrottle(uid="my-api", rate="100/min", identifier=identifier)
```

`get_remote_address` only reads a proxy header when the connection's immediate
peer matches one of `trusted_proxies`. Otherwise, the raw peer address is
returned, completely unaffected by header content - so a client that isn't
coming through your proxy can't influence the result no matter what it sends.

### `trusted_proxies`: addresses and/or CIDR networks

`trusted_proxies` takes any mix of `ipaddress` addresses and networks (the
`TrustedProxy` type):

```python
import ipaddress

trusted_proxies = (
    ipaddress.ip_address("10.0.0.1"),      # a single, fixed proxy IP
    ipaddress.ip_network("10.1.0.0/16"),   # a whole subnet - e.g. an internal LB fleet
    ipaddress.ip_address("2001:db8::1"),   # IPv6 works the same way
)
```

A single trusted address is checked with an O(1) lookup; CIDR networks are only
checked by containment if the peer isn't an exact match. Chains of multiple
trusted proxies are also handled. `X-Forwarded-For` is walked from the nearest
hop backwards, skipping over any entry that is itself a trusted proxy, until it
finds the first address that isn't and that's treated as the real client, and
nothing further back in the header is trusted or even read.

!!! tip "Pass the same object every time"
    Trusted-proxy checking is cached per distinct `trusted_proxies` value. Build
    the tuple once at module or config level and reuse it, rather than
    constructing a new list on every request. `functools.partial` in the
    examples above already does this for you, since the tuple is bound once
    when the partial is created.

### `ProxyHeaders`: which header convention to read

```python
from traffik import ProxyHeaders

ProxyHeaders.FORWARDED         # RFC 7239 `Forwarded` header
ProxyHeaders.X_FORWARDED_FOR   # `X-Forwarded-For` (the default)
ProxyHeaders.X_REAL_IP         # `X-Real-IP` (common with nginx)
ProxyHeaders.TRUE_CLIENT_IP    # `True-Client-IP` (some CDNs)
ProxyHeaders.CF_CONNECTING_IP  # `CF-Connecting-IP` (Cloudflare)
ProxyHeaders.ALL               # consult all of the above
```

These are flags, so combine only the ones your deployment actually sets:

```python
proxy_headers = ProxyHeaders.X_FORWARDED_FOR | ProxyHeaders.X_REAL_IP
```

When more than one is enabled and present, they're checked in this order:
`Forwarded`, then `X-Forwarded-For`, then `CF-Connecting-IP`, `True-Client-IP`,
`X-Real-IP`. The first valid IP found wins.

!!! tip "Only enable the headers your proxy actually sets"
    If you're only ever behind Cloudflare, enable just `ProxyHeaders.CF_CONNECTING_IP`.
    There's no benefit to also enabling `X-Forwarded-For` if nothing in your
    infrastructure ever sets it. It's one less header format to reason about.

### Common setups

=== "Single reverse proxy, same host"

    ```python
    import functools
    import ipaddress
    from traffik import ProxyHeaders, get_remote_address

    identifier = functools.partial(
        get_remote_address,
        trusted_proxies=(ipaddress.ip_address("127.0.0.1"),),
        proxy_headers=ProxyHeaders.X_FORWARDED_FOR,
    )
    ```

=== "Internal load balancer fleet"

    ```python
    import functools
    import ipaddress
    from traffik import ProxyHeaders, get_remote_address

    identifier = functools.partial(
        get_remote_address,
        trusted_proxies=(ipaddress.ip_network("10.0.0.0/16"),),
        proxy_headers=ProxyHeaders.X_FORWARDED_FOR,
    )
    ```

=== "Behind Cloudflare"

    ```python
    import functools
    from traffik import ProxyHeaders, get_remote_address

    # Fetch and normalize Cloudflare's published edge IP ranges at startup:
    # https://www.cloudflare.com/ips/
    from myapp.config import CLOUDFLARE_IP_RANGES

    identifier = functools.partial(
        get_remote_address,
        trusted_proxies=CLOUDFLARE_IP_RANGES,
        proxy_headers=ProxyHeaders.CF_CONNECTING_IP,
    )
    ```

    !!! warning "Trust Cloudflare's edge, not just the header name"
        Enabling `CF_CONNECTING_IP` isn't sufficient on its own. Anyone can set
        that header directly if your origin is reachable without going through
        Cloudflare. `trusted_proxies` must be Cloudflare's actual published IP
        ranges (or your own proxy sitting in front of it), never left empty
        just because you're "using Cloudflare".

=== "Chain of trusted proxies"

    ```python
    import functools
    import ipaddress
    from traffik import ProxyHeaders, get_remote_address

    # e.g. client -> internal load balancer -> internal reverse proxy -> app
    identifier = functools.partial(
        get_remote_address,
        trusted_proxies=(
            ipaddress.ip_address("10.0.0.1"),
            ipaddress.ip_address("10.0.0.2"),
        ),
        proxy_headers=ProxyHeaders.X_FORWARDED_FOR,
    )
    ```

---

## Custom identifiers

An identifier is any `async` function that takes an `HTTPConnection` and returns
something that can be converted to a string, to distinguish connections/clients:

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
    from traffik import get_remote_address
    from traffik.config import ANONYMOUS_IDENTIFIER

    async def ip_identifier(connection: HTTPConnection) -> str:
        # No trusted_proxies -> always the raw socket peer, headers ignored.
        # See "Behind a reverse proxy" above if you're behind one.
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
import functools

from starlette.requests import HTTPConnection
from traffik import EXEMPTED, ProxyHeaders, get_remote_address

# Built once, at startup - see "Behind a reverse proxy" above.
get_client_ip = functools.partial(
    get_remote_address,
    trusted_proxies=YOUR_TRUSTED_PROXIES,
    proxy_headers=ProxyHeaders.X_FORWARDED_FOR,
)

async def identifier_with_allowlist(connection: HTTPConnection):
    ip = get_client_ip(connection)

    # Internal health checkers, CI runners, etc.
    if ip in {"10.0.0.1", "10.0.0.2"}:
        return EXEMPTED  # this connection is completely untouched

    return ip
```

!!! warning "Never build an allowlist from a raw header read"
    An allowlist is a *complete* bypass of throttling, so it's the worst possible
    place to trust a header without verifying it came from somewhere you trust.
    Reading `connection.headers.get("x-forwarded-for")` directly here would let
    any client claim to be `10.0.0.1` and exempt itself from rate limiting
    entirely. Always go through `get_remote_address` with `trusted_proxies`
    configured, as above.

!!! tip "EXEMPTED is the right tool for internal traffic"
    Allowlisting via `EXEMPTED` costs nothing. Returning an identifier that you
    then map to an unlimited `Rate` does same but has some overhead.
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

By default (`cache_ids=True`), Traffik calls your identifier function **once per request** and caches the result in the request's context for the lifetime of that request. If the throttle is applied or hit multiple time on the same route or endpoint, they all reuse the cached value and your identifier function is never called more than once.

```python
from traffik import HTTPThrottle

throttle = HTTPThrottle(
    uid="my-api",
    rate="100/min",
    identifier=my_identifier,
    cache_ids=True,   # default - identifier called once, result reused
)
```

Set `cache_ids=False` only if your identifier function's result can legitimately
change between throttle invocations within the same request (which is almost never
the case).

!!! warning "Avoid slow I/O in identifier functions on hot paths"
    Your identifier runs on **every request**. An identifier that makes a database
    query or an external HTTP call to resolve a user ID will add that latency to
    every single request that hits any throttle. Consider caching resolved identities
    in `request.state` or a cache backend yourself, or keep the identifier to headers and path
    parameters only.

    If you must do I/O, at least make sure `cache_ids=True` (the default) so the
    I/O only happens once per request, not once per throttle per request.
