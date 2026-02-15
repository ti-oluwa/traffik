# Throttle Rules & Wildcards

Throttles apply globally by default. Attach one to a router and it runs on every request that hits that router - no questions asked. That's perfect for simple cases, but real APIs rarely have simple cases.

What if your global API throttle should apply to *most* routes but not the health check? What if `GET /users` has its own stricter limit and shouldn't also drain the shared pool? What if you want one throttle to govern anonymous users and a totally different one for premium accounts?

That's what **throttle rules** are for. Rules let a throttle ask "does this connection even apply to me?" before doing anything - before touching the backend, before computing the client identifier, before any of it. If the rule says no, the throttle steps aside cleanly.

---

## The Problem: Selective Throttling

Here's the scenario that rules are designed for. Imagine a versioned API:

```
/api/v1                  (GET: 1000/min, POST: 300/min)
  /api/v1/users          (GET: 500/min,  POST: unlimited)
  /api/v1/organizations  (GET: unlimited, POST: 600/min)
    /api/v1/organizations/{id}  (GET: 100/min additional limit)
```

The global throttle covers everything under `/api/v1`. But:

- `GET /api/v1/users` has its own 500/min limit, so it shouldn't *also* eat from the 1000/min global pool for GETs.
- `POST /api/v1/organizations` has its own 600/min limit, so it shouldn't *also* count against the global 300/min POST limit.

Without rules, you'd have to manually split your routing, duplicate logic, or accept over-counting. With rules, you express exactly this in a few lines.

---

## `ThrottleRule` — "Only apply when..."

A `ThrottleRule` is a gate: the throttle applies **only** when the connection matches the rule. Non-matching connections pass straight through without consuming any quota.

```python
from traffik.registry import ThrottleRule

# Only throttle GET requests to /api/users
rule = ThrottleRule(path="/api/users", methods={"GET"})
throttle = HTTPThrottle("api:users", rate="500/min", rules={rule})
```

A `ThrottleRule` accepts three optional parameters - all are conjunctive:

- **`path`** - a path pattern (string, glob, or compiled regex). The connection's path must match.
- **`methods`** - a set of HTTP method strings (e.g. `{"GET", "POST"}`). The connection's method must be in the set. Case-insensitive.
- **`predicate`** - an async callable for custom logic. Must return `True` for the throttle to apply.

If *any* of the specified criteria don't match, the throttle is skipped for that connection. All criteria that *are* specified must pass.

---

## `BypassThrottleRule` — "Skip when..."

`BypassThrottleRule` is the inverse: the throttle is skipped **when** the connection matches.

```python
from traffik.registry import BypassThrottleRule

# Global throttle applies to everything EXCEPT GET /api/users
bypass = BypassThrottleRule(path="/api/users", methods={"GET"})
global_throttle = HTTPThrottle("api:global", rate="1000/min", rules={bypass})
```

This is often more ergonomic for global throttles with carve-outs. Rather than listing every path that *should* be throttled (which changes as you add routes), you list the exceptions.

!!! tip "ThrottleRule vs BypassThrottleRule: which to reach for?"
    Use `ThrottleRule` when you're building a *targeted* throttle that should only apply to specific routes - e.g., a strict limit on the login endpoint.

    Use `BypassThrottleRule` when you have a *broad* throttle (e.g., a global router-level limit) and want to carve out exceptions - e.g., "everything except health checks and the CDN callback."

---

## Wildcard Path Patterns

The `path` parameter on both rule types accepts glob-style wildcards:

| Pattern | Matches | Does not match |
|---|---|---|
| `/api/*` | `/api/users`, `/api/v1` | `/api/v1/users` (contains nested `/`) |
| `/api/**` | `/api/users`, `/api/v1/users`, `/api/a/b/c` | `/other/path` |
| `/api/v*/users` | `/api/v1/users`, `/api/v2/users` | `/api/v1/admins` |
| `/api/users` | `/api/users`, `/api/users/dashboard` | `/other/users` |

The rules are:

- **`*`** matches a single path segment - everything except `/`. Useful for matching one level of path hierarchy.
- **`**`** matches multiple segments including `/`. Useful for "everything under this prefix".
- **Plain strings** (no `*`) are treated as prefix regex matches - so `/api/users` matches `/api/users`, `/api/users/123`, `/api/users/dashboard`, etc.
- **Compiled `re.Pattern`** objects are matched as-is against the full connection path.

```python
import re
from traffik.registry import BypassThrottleRule, ThrottleRule

# Glob wildcards
BypassThrottleRule(path="/api/v*/users")    # /api/v1/users, /api/v2/users, etc.
BypassThrottleRule(path="/api/**")           # Anything under /api/

# Compiled regex (exact control)
ThrottleRule(path=re.compile(r"^/api/users/\d+$"))  # Only numeric user IDs

# Prefix match (no wildcards)
BypassThrottleRule(path="/api/internal")  # /api/internal, /api/internal/health, etc.
```

!!! note "Regex vs prefix matching"
    When you pass a plain string with no `*`, Traffik compiles it as a regex and uses `re.Pattern.match()`, which matches from the start of the string. Since `match()` doesn't require matching to the end, `/api/users` becomes a prefix match. If you need a full-string match, use a compiled regex with `$` at the end.

!!! tip "`ThrottleMiddleware` path uses `ThrottleRule` underneath"
    The `path` parameter on `MiddlewareThrottle` (used with `ThrottleMiddleware`) uses the same `ThrottleRule` path-matching logic under the hood. That means it supports the same wildcard patterns — `*`, `**`, plain string prefix match, and compiled `re.Pattern`. See [Middleware](../integration/middleware.md) for usage examples.

---

## Attaching Rules at Initialization

Pass a `rules` set when constructing the throttle:

```python
from traffik import HTTPThrottle
from traffik.registry import BypassThrottleRule

bypass1 = BypassThrottleRule(path="/api/users", methods={"GET"})
bypass2 = BypassThrottleRule(path="/api/organizations", methods={"POST"})

global_throttle = HTTPThrottle(
    "api:v1",
    rate="1000/min",
    rules={bypass1, bypass2},  # Both bypass rules are attached at creation
)
```

Rules passed here are attached directly to this throttle. They're deduplicated and sorted for optimal evaluation order automatically.

---

## Adding Rules After Initialization

You can also attach rules to *any* registered throttle by its UID after the fact, using `add_rules()` on any throttle instance:

```python
from traffik import HTTPThrottle
from traffik.registry import BypassThrottleRule

global_throttle = HTTPThrottle("api:v1", rate="1000/min")

# Later, when setting up the users router:
users_throttle = HTTPThrottle("api:users", rate="500/min")

bypass_GET_users = BypassThrottleRule(path="/api/v1/users", methods={"GET"})
users_throttle.add_rules("api:v1", bypass_GET_users)
# ^ Attaches the rule to global_throttle (uid="api:v1"), not to users_throttle
```

Notice the `add_rules` call: the first argument is the **target throttle's UID** - the throttle you want to *modify*. The rule is added to the global registry, and the target throttle will pick it up on its next `hit()` call.

!!! warning "UIDs must be registered first"
    `add_rules("api:v1", ...)` will raise a `ConfigurationError` if no throttle with UID `"api:v1"` has been created yet. Create throttles before attaching rules to them.

By default, rules added via `add_rules()` are picked up on the first request after they're added and cached for efficiency. If you need rules to be re-fetched on every single request (rare), set `dynamic_rules=True` on the throttle.

---

## Custom Predicate Rules

For conditions that path and methods can't express - like "only throttle premium users", or "skip throttling for internal service calls" - use a `predicate`:

```python
from traffik.registry import ThrottleRule, BypassThrottleRule

# Only apply this throttle to users on the "premium" tier
async def only_premium(connection) -> bool:
    user = getattr(connection.state, "user", None)
    if user is None:
        return False
    return user.tier == "premium"

premium_rule = ThrottleRule(predicate=only_premium)
premium_throttle = HTTPThrottle("api:premium", rate="5000/min", rules={premium_rule})
```

Predicates can optionally accept a `context` argument:

```python
async def only_premium_with_context(connection, context=None) -> bool:
    # context contains the throttle's merged context dict
    tier = (context or {}).get("user_tier")
    return tier == "premium"
```

Traffik inspects the predicate's signature and passes `context` only if the function accepts it.

---

## Rule Evaluation Order

Rules are evaluated in an order optimised for short-circuit performance. Traffik automatically sorts them:

1. **`BypassThrottleRule` without predicate** - fastest path; a frozenset lookup + regex match. Returns `False` on match (throttle skipped immediately).
2. **`ThrottleRule` without predicate** - same cost as above. Returns `False` on non-match (throttle skipped immediately).
3. **`BypassThrottleRule` with predicate** - async; predicate runs after path/method check passes.
4. **`ThrottleRule` with predicate** - async; slowest. Runs only if all cheaper rules passed.

You don't need to think about this ordering - Traffik handles it. But it's good to know why **cheap method/path rules should always be preferred over predicates** when they can express the same condition.

!!! note "`BypassThrottleRule` always wins first"
    `BypassThrottleRule` instances are always checked **before** `ThrottleRule` instances within the same priority tier. This short-circuit means: if any bypass rule matches, the throttle is skipped immediately without evaluating any `ThrottleRule` predicates. Keep bypass rules cheap and tight — they protect everything downstream.

---

## The `ThrottleRegistry` and `GLOBAL_REGISTRY`

Every throttle registers itself in the `GLOBAL_REGISTRY` — an instance of `ThrottleRegistry` — on construction (unless you pass a custom `registry`). This is what makes `add_rules()` work across module boundaries without passing references around.

```python
from traffik import HTTPThrottle
from traffik.registry import GLOBAL_REGISTRY, ThrottleRegistry

# Check if a throttle has been registered
GLOBAL_REGISTRY.exist("api:v1")   # True if HTTPThrottle("api:v1", ...) was called
GLOBAL_REGISTRY.exist("api:v99")  # False

# Add rules directly through the registry
GLOBAL_REGISTRY.add_rules("api:v1", bypass_rule)

# You can also create isolated registries for testing or multi-tenant setups
custom_registry = ThrottleRegistry()
throttle = HTTPThrottle("api:v1", rate="100/min", registry=custom_registry)
```

The registry uses a re-entrant lock internally, so it's safe to register throttles and add rules from different threads during application startup.

!!! note "UID uniqueness"
    Throttle UIDs must be globally unique within a registry. If you try to create two `HTTPThrottle` instances with the same UID, the second one raises a `ConfigurationError`. This is intentional - shared UIDs would cause state collisions in the backend.

---

## Full Real-World Example

Here's the complete pattern for a versioned API with a global throttle and per-router carve-outs - adapted from the Traffik examples:

```python
import typing
from fastapi import APIRouter, Depends, FastAPI, Request

from traffik import HTTPThrottle, Rate
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators import throttled
from traffik.registry import BypassThrottleRule

app = FastAPI(lifespan=InMemoryBackend().lifespan)

# ─── GLOBAL THROTTLE ────────────────────────────────────────────────────────
# GET: 1000/min, POST: 300/min for everything under /api/v1
# Exceptions are carved out below with BypassThrottleRules.

async def global_rate(
    connection: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> Rate:
    if connection.scope["method"] == "GET":
        return Rate.parse("1000/min")
    return Rate.parse("300/min")


# Carve-outs: global throttle should NOT apply to these
bypass_GET_users = BypassThrottleRule(path="/api/v1/users", methods={"GET"})
bypass_POST_orgs = BypassThrottleRule(path="/api/v1/organizations", methods={"POST"})

global_throttle = HTTPThrottle(
    "api:v1",
    rate=global_rate,
    rules={bypass_GET_users, bypass_POST_orgs},  # (1)!
)
main_router = APIRouter(prefix="/api/v1", dependencies=[Depends(global_throttle)])


# ─── HELPER: resolve user ID from state ─────────────────────────────────────

async def get_user_id(connection: Request) -> str:
    return getattr(connection.state, "user_id", "__anon__")


# ─── USERS ROUTER ────────────────────────────────────────────────────────────
# GET: 500/min (per-user), POST: unlimited

async def user_rate(
    connection: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> Rate:
    if connection.scope["method"] == "GET":
        return Rate.parse("500/min")
    return Rate()  # Rate() with no args = unlimited = no throttling


users_throttle = HTTPThrottle(
    "api:users",
    rate=user_rate,
    identifier=get_user_id,  # Per-user tracking (2)!
)
users_router = APIRouter(prefix="/users", dependencies=[Depends(users_throttle)])


# ─── ORGANIZATIONS ROUTER ────────────────────────────────────────────────────
# GET: unlimited, POST: 600/min (per-user)

async def orgs_rate(
    connection: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> Rate:
    if connection.scope["method"] == "POST":
        return Rate.parse("600/min")
    return Rate()  # Unlimited for GET


orgs_throttle = HTTPThrottle(
    "api:orgs",
    rate=orgs_rate,
    identifier=get_user_id,
)
orgs_router = APIRouter(
    prefix="/organizations",
    dependencies=[Depends(orgs_throttle)],
)


@orgs_router.post("/")
async def create_organization(data: typing.Any):
    # Only orgs_throttle applies (POST; global is bypassed)
    pass


@orgs_router.get("/{org_id}")
@throttled(
    HTTPThrottle("api:orgs:get", rate="100/min")  # (3)!
)
async def get_organization(org_id: str):
    # orgs_throttle + api:orgs:get throttle both apply here
    pass


# ─── ASSEMBLE THE APP ────────────────────────────────────────────────────────
main_router.include_router(users_router)
main_router.include_router(orgs_router)
app.include_router(main_router)
```

1. Rules are passed at construction here. Alternatively, you can call `users_throttle.add_rules("api:v1", bypass_GET_users)` after the fact - both approaches produce identical results.
2. Without `identifier=get_user_id`, the throttle would use the remote IP address. Per-user throttling ensures authenticated users each get their own 500/min quota rather than sharing a per-IP pool.
3. Using `@throttled(...)` stacks an additional throttle on top of the router-level `orgs_throttle`. Both run on `GET /{org_id}`. Use this for routes that need stricter limits than their parent router.

### What happens on each route

| Route | Throttles that apply |
|---|---|
| `GET /api/v1/users` | `api:users` only (global bypassed) |
| `POST /api/v1/users` | `api:v1` global (300/min) + `api:users` unlimited |
| `GET /api/v1/organizations` | `api:v1` global (1000/min) + `api:orgs` unlimited |
| `POST /api/v1/organizations` | `api:orgs` (600/min) only (global bypassed) |
| `GET /api/v1/organizations/{id}` | `api:v1` global (1000/min) + `api:orgs` unlimited + `api:orgs:get` (100/min) |

---

## Choosing the Right Tool

There are three ways to make a throttle apply differently to different connections. Here's when to reach for each:

=== "Rules"

    **Best when:** The condition is based on the path or HTTP method. Structural routing concerns.

    ```python
    # Apply only to admin routes
    ThrottleRule(path="/api/admin/**")

    # Skip for health checks
    BypassThrottleRule(path="/health")
    ```

    Rules are evaluated before any backend I/O. Zero quota is consumed when a rule skips a throttle. They're the cheapest way to make structural decisions.

=== "Dynamic Identifiers"

    **Best when:** You want the *same* throttle to track different groups of clients separately by returning different identifier strings.

    ```python
    async def tier_identifier(connection: Request) -> str:
        user = connection.state.user
        return f"{user.tier}:{user.id}"
    # premium:42 and free:42 get separate counters
    ```

    Use this when the throttle logic is the same but you want per-group accounting - e.g., premium vs free tier users each get their own 1000/min pool.

=== "Predicate Rules"

    **Best when:** The condition involves async logic or data not available from the path/method alone - like user tier, feature flags, or tenant config.

    ```python
    async def only_external(connection) -> bool:
        return not getattr(connection.state, "is_internal", False)

    ThrottleRule(predicate=only_external)
    ```

    Predicates run after path/method checks and involve an `await`. Use them for cross-cutting concerns that can't be expressed structurally.

!!! tip "Combine freely"
    Rules, identifiers, and predicates compose. A single throttle can have multiple bypass rules (e.g., one for `/health`, one for internal IPs) *and* a custom identifier *and* a predicate - all working together. Traffik evaluates them in the correct order automatically.

---

## Summary

| Concept | What it does |
|---|---|
| `ThrottleRule(path, methods, predicate)` | Apply throttle **only** when connection matches |
| `BypassThrottleRule(path, methods, predicate)` | Skip throttle **when** connection matches |
| `BypassThrottleRule` checked first | Short-circuits before any `ThrottleRule` in the same tier |
| `rules={...}` in constructor | Attach rules at throttle creation |
| `throttle.add_rules("uid", rule)` | Attach rules to another throttle by UID after creation |
| `*` in path pattern | Matches one path segment (no `/`) |
| `**` in path pattern | Matches multiple segments (including `/`) |
| Plain string path | Prefix regex match |
| `re.compile(...)` path | Exact regex match |
| `ThrottleRegistry` | The class backing `GLOBAL_REGISTRY`; pass a custom instance via `registry=` |
| `GLOBAL_REGISTRY.exist("uid")` | Check if a throttle UID is registered |
| `dynamic_rules=True` | Re-fetch registry rules on every request |
