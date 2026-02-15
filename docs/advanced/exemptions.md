# Exemptions

Some clients should never hit a rate limit. Your internal health check service.
Your admin dashboard. A premium user on a contract with a dedicated SLA. A
whitelisted IP for a partner integration.

Traffik handles this cleanly through the `EXEMPTED` sentinel. When your identifier
function returns `EXEMPTED`, Traffik short-circuits the entire throttle pipeline:
no counter is incremented, no backend is consulted, no lock is acquired. The request
passes through as if the throttle wasn't there.

---

## The `EXEMPTED` Sentinel

`EXEMPTED` is a special singleton value exported from `traffik`:

```python
from traffik import EXEMPTED
```

Return it from your identifier function to exempt a connection from throttling:

```python
from traffik import EXEMPTED, HTTPThrottle
from starlette.requests import Request

async def my_identifier(request: Request):
    if some_condition(request):
        return EXEMPTED  # This connection is exempt
    return request.client.host  # Everyone else: throttled by IP
```

!!! tip "Zero overhead"
    When `EXEMPTED` is returned, Traffik exits immediately. No backend call is made,
    no lock is acquired. It's an extremely efficient way to bypass throttling. Note
    that `cost=0` is actually checked *before* the identifier, so a zero-cost request
    never even reaches the identifier stage — but `EXEMPTED` skips everything that
    follows the identifier, including the backend lookup.

---

## How It Works

Traffik's `hit(...)` method evaluates exemptions in a specific order:

1. **Cost check** — if `cost=0` is resolved (e.g., via a dynamic cost function returning `0`), the hit is a no-op: no backend call, no counter increment. The connection passes through.
2. **Identifier check** — the identifier function is called. If the result is `EXEMPTED` — stop. Return the connection untouched.
3. Otherwise, proceed with backend lookup and counter increment.

Step 2 is the key short-circuit: the entire downstream machinery simply never runs when `EXEMPTED` is returned. Note that `cost=0` is checked first (step 1) — this means a zero-cost hit never even reaches the identifier stage.

---

## Common Exemption Patterns

### Exempt by Token (Admin Access)

Check a header for an admin token. Admins get through unconditionally; everyone
else is throttled by user ID.

```python
from traffik import EXEMPTED, HTTPThrottle, default_identifier
from starlette.requests import Request

ADMIN_SECRET = "super-secret-admin-token"  # Load from env in practice

async def admin_aware_identifier(request: Request):
    if request.headers.get("x-admin-token") == ADMIN_SECRET:
        return EXEMPTED
    # Fall back to the default (remote IP address)
    return await default_identifier(request)

throttle = HTTPThrottle(
    uid="api:public",
    rate="100/min",
    identifier=admin_aware_identifier,
)
```

### Exempt by User Tier (Premium vs Free)

Load the user from your database (or a JWT claim) and branch on their tier:

```python
from traffik import EXEMPTED, HTTPThrottle
from starlette.requests import Request

async def tiered_identifier(request: Request):
    user = getattr(request.state, "user", None)

    if user is None:
        # Unauthenticated — throttle by IP
        return request.client.host or "anonymous"

    if user.tier == "enterprise":
        return EXEMPTED          # Enterprise: no limit

    if user.tier == "premium":
        return f"premium:{user.id}"   # Premium: separate (generous) bucket

    return f"free:{user.id}"          # Free: the default (strict) bucket

throttle = HTTPThrottle(
    uid="api:tiered",
    rate="60/min",   # Applies to free and premium users; enterprise skips entirely
    identifier=tiered_identifier,
)
```

### Exempt by IP Allowlist (Internal Services)

Internal services that call your API from known IP ranges shouldn't consume quota:

```python
from traffik import EXEMPTED, HTTPThrottle
from starlette.requests import Request
import ipaddress

INTERNAL_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]

def is_internal_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in INTERNAL_NETWORKS)
    except ValueError:
        return False

async def allowlist_identifier(request: Request):
    client_ip = request.client.host or ""
    if is_internal_ip(client_ip):
        return EXEMPTED
    return client_ip

internal_throttle = HTTPThrottle(
    uid="api:external-only",
    rate="200/min",
    identifier=allowlist_identifier,
)
```

### A Full Tiered Example

This identifier combines all three approaches: admin tokens, tier-based logic, and
a fallback to IP for unauthenticated users.

```python
from traffik import EXEMPTED, HTTPThrottle, default_identifier
from starlette.requests import Request

async def smart_identifier(request: Request):
    # 1. Internal admin token? Exempt entirely.
    if request.headers.get("x-admin-token") == ADMIN_TOKEN:
        return EXEMPTED

    user = getattr(request.state, "user", None)

    # 2. No authenticated user? Fall back to IP.
    if user is None:
        return await default_identifier(request)

    # 3. Enterprise tier? Exempt.
    if user.plan == "enterprise":
        return EXEMPTED

    # 4. Everyone else: throttle by user ID.
    return f"user:{user.id}"

api_throttle = HTTPThrottle(
    uid="api:main",
    rate="100/min",
    identifier=smart_identifier,
)
```

---

## Alternative Exemption Mechanisms

`EXEMPTED` is the cleanest way to skip throttling for a specific client, but it's
not the only tool in the box. Two lighter-weight alternatives are worth knowing:

### `cost=0` — Silent Pass-Through

Setting `cost=0` on a throttle call (or returning `0` from a dynamic cost function)
causes Traffik to skip the entire backend operation for that hit — no counter is
incremented, no lock is acquired. The check happens *before* the identifier is even
called, making it the cheapest possible path through the throttle:

```python
from traffik import HTTPThrottle

async def dynamic_cost(connection, context=None) -> int:
    # Internal health check probes carry no cost
    if connection.headers.get("x-internal-probe") == "1":
        return 0
    return 1

throttle = HTTPThrottle(uid="api:main", rate="100/min", cost=dynamic_cost)
```

This is more efficient than `EXEMPTED` for cases where you don't need to identify
the client at all — the cost check short-circuits before the identifier runs.

### Very Low `rate` — Soft Throttle / Soft Allow

Setting a very high limit (e.g. `rate="1000000/min"`) effectively gives a client
unlimited capacity without fully exempting them. They still go through the full
throttle pipeline and still consume quota — just at a rate that will never realistically
be hit. This is useful when you want throttle telemetry (statistics, headers) for a
client but don't want to restrict them:

```python
from traffik import HTTPThrottle, Rate

async def effective_rate(connection, context=None) -> Rate:
    user = getattr(connection.state, "user", None)
    if user and user.tier == "enterprise":
        return Rate.parse("1000000/min")  # Effectively unlimited, but tracked
    return Rate.parse("100/min")

throttle = HTTPThrottle(uid="api:tiered", rate=effective_rate)
```

!!! tip "Which to reach for?"
    - Use `EXEMPTED` when a client should produce zero backend activity — health checks, internal services, admin tokens.
    - Use `cost=0` when you want to suppress quota consumption based on request content (headers, payload type) rather than client identity.
    - Use a very high `rate` when you want the client tracked in telemetry but never blocked.

---

## `EXEMPTED` vs `BypassThrottleRule`

Traffik offers two ways to skip throttling, and they're designed for different
situations:

| | `EXEMPTED` (from identifier) | `BypassThrottleRule` |
|---|---|---|
| **Decision based on** | Who the client is (user, IP, token) | What the request is (path, method) |
| **Configured on** | The `identifier` function | The throttle's `rules` parameter |
| **Overhead** | Near zero — stops at identifier stage | Near zero — stops at rule check stage |
| **Use for** | Trusted clients, premium users, admin tokens | Paths or methods that should never be throttled |

!!! tip "Path-based exemptions: use `BypassThrottleRule`"
    If you want to skip throttling for `GET /health` or `GET /metrics`, you should
    use `BypassThrottleRule` rather than encoding path logic in your identifier:

    ```python
    from traffik.registry import BypassThrottleRule

    throttle = HTTPThrottle(
        uid="api:main",
        rate="100/min",
        rules={
            BypassThrottleRule(path="/health", methods={"GET"}),
            BypassThrottleRule(path="/metrics", methods={"GET"}),
        },
    )
    ```

    This keeps your identifier focused on *who* the client is, and your rules
    focused on *what* the request is.

---

## Important: `EXEMPTED` Affects All Throttles Using That Identifier

When an identifier returns `EXEMPTED`, that exemption applies to the specific
throttle whose identifier is being called — not globally across all throttles.

If you have two throttles that share the same identifier function, and that function
returns `EXEMPTED` for a given client, both throttles exempt that client. This is
usually what you want: a true VIP client skips every limit.

If you want selective exemptions — skip throttle A but not throttle B for the same
client — use separate identifier functions, or use `BypassThrottleRule` on the
specific throttle you want to bypass.

```python
# Throttle A: exempts admins entirely
throttle_a = HTTPThrottle("api:general", rate="100/min", identifier=admin_aware_id)

# Throttle B: admins still subject to this limit (audit trail, etc.)
throttle_b = HTTPThrottle("api:audit",   rate="1000/min", identifier=default_identifier)
```
