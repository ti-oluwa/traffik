# Throttle Registry

Every throttle in Traffik belongs to a `ThrottleRegistry`. The registry is the coordination layer that holds throttle memberships, attaches rules, and lets you disable or re-enable throttles at runtime without touching application code.

---

## What is the ThrottleRegistry?

`ThrottleRegistry` is a lightweight, thread-safe container that:

- Tracks which throttle UIDs are active ("registered").
- Stores the `ThrottleRule` / `BypassThrottleRule` sets that gate each throttle's `hit()` call.
- Keeps a weak reference to each throttle instance so it can forward `disable()` / `enable()` calls without preventing garbage collection.

```python
from traffik.registry import ThrottleRegistry

registry = ThrottleRegistry()
```

A module-level singleton `GLOBAL_REGISTRY` is the default registry used when you don't pass one explicitly:

```python
from traffik.registry import GLOBAL_REGISTRY
```

---

## How throttles register

Throttles register themselves automatically when they are created. You never need to call `registry.register()` manually. When the throttle is garbage-collected, it is automatically unregistered via a `weakref.finalize` callback.

```python
from traffik import HTTPThrottle
from traffik.registry import ThrottleRegistry

registry = ThrottleRegistry()

# Automatically registered on construction, unregistered on GC
throttle = HTTPThrottle("api:v1", rate="100/min", registry=registry)

registry.exist("api:v1")   # True
```

---

## Sharing a registry

Multiple throttles sharing a single registry lets you manage them as a group: attach rules to several throttles at once, or disable them all in one call.

```python
from traffik import HTTPThrottle
from traffik.registry import ThrottleRegistry

registry = ThrottleRegistry()

read_throttle  = HTTPThrottle("api:read",  rate="200/min", registry=registry)
write_throttle = HTTPThrottle("api:write", rate="50/min",  registry=registry)
admin_throttle = HTTPThrottle("api:admin", rate="500/min", registry=registry)
```

---

## Adding rules

`add_rules()` attaches `ThrottleRule` / `BypassThrottleRule` instances to a throttle by UID. Rules are checked conjunctively on every `hit()` call. If any rule returns `False`, the throttle is skipped for that request.

```python
from traffik.registry import ThrottleRule, BypassThrottleRule

# Only apply the write throttle on POST/PUT/DELETE
registry.add_rules(
    "api:write",
    ThrottleRule(methods={"POST", "PUT", "DELETE"}),
)

# Exempt health checks from all throttles in the registry
for uid in ("api:read", "api:write", "api:admin"):
    registry.add_rules(uid, BypassThrottleRule(path="/health"))
```

See [Throttle Rules & Wildcards](rules.md) for the full path-matching and predicate API.

---

## Disable and enable

### Per-throttle

Call `disable()` / `enable()` directly on the throttle instance. Both are async and acquire the throttle's internal update lock, so they are safe to call concurrently with `hit()`.

```python
# Disable a throttle — subsequent hit() calls return immediately without consuming quota
await throttle.disable()

# Check status
throttle.is_disabled   # True

# Re-enable
await throttle.enable()
throttle.is_disabled   # False
```

A common use-case is disabling a throttle from an error handler when the backend becomes unavailable:

```python
from traffik import HTTPThrottle

async def my_on_error(connection, exc_info):
    throttle = exc_info["throttle"]
    await throttle.disable()   # Let all traffic through while backend recovers
    return 0                   # Allow this request

rate_throttle = HTTPThrottle(
    "api:v1",
    rate="100/min",
    on_error=my_on_error,
)
```

### Via registry

Use `registry.disable(uid)` when you only have access to the registry (e.g., from a management endpoint or startup hook):

```python
found = await registry.disable("api:write")   # True if throttle is alive
found = await registry.enable("api:write")    # True if throttle is alive
```

Both methods return `True` if the throttle was found and acted upon, or `False` if the UID is unknown or the instance has been garbage-collected.

### All at once

`disable_all()` and `enable_all()` iterate every live throttle in the registry:

```python
# Emergency kill switch — let all traffic through
await registry.disable_all()

# Resume normal throttling
await registry.enable_all()
```

Dead (GC'd) throttles are silently skipped.

---

## Runtime updates

Each mutable throttle property has its own `update_*()` method. All are async and acquire the same update lock as `disable()` / `enable()`, so concurrent callers always see a consistent state.

```python
from traffik import HTTPThrottle
from traffik.headers import DEFAULT_HEADERS_ALWAYS

# Change rate limit (accepts a string, Rate object, or async callable)
await throttle.update_rate("200/min")

# Swap backend (e.g., after hot-reload)
await throttle.update_backend(new_backend)

# Swap strategy
from traffik.strategies.token_bucket import TokenBucketStrategy
await throttle.update_strategy(TokenBucketStrategy())

# Adjust cost
await throttle.update_cost(2)

# Change minimum wait floor
await throttle.update_min_wait_period(500)  # 500 ms

# Replace throttled-response handler
await throttle.update_handle_throttled(my_handler)

# Replace response headers
await throttle.update_headers(DEFAULT_HEADERS_ALWAYS)

# Replace identifier function (changes how connections are keyed)
await throttle.update_identifier(new_identifier_fn)
```

Properties that are intentionally immutable, `uid` and `registry`, have no update method. Changing either would break registry membership and rule lookups.

---

## Inspecting the registry

```python
from traffik.registry import ThrottleRegistry

registry = ThrottleRegistry()

# Check if a UID is registered
registry.exist("api:v1")          # True / False

# Retrieve attached rules
rules = registry.get_rules("api:v1")   # List[ThrottleRule]

# Retrieve the live throttle instance (or None if GC'd)
throttle = registry.get_throttle("api:v1")

# Wipe everything (unregisters all UIDs, rules, and refs)
registry.clear()
```

---

## Summary

| Method / attribute | What it does |
|---|---|
| `registry.exist(uid)` | Check if a UID is registered |
| `registry.add_rules(uid, *rules)` | Attach rules to a throttle |
| `registry.get_rules(uid)` | Retrieve all rules for a throttle |
| `registry.get_throttle(uid)` | Return the live throttle instance, or `None` |
| `registry.disable(uid)` | Disable a specific throttle (async) |
| `registry.enable(uid)` | Re-enable a specific throttle (async) |
| `registry.disable_all()` | Disable every live throttle in the registry (async) |
| `registry.enable_all()` | Re-enable every live throttle in the registry (async) |
| `registry.clear()` | Wipe all registrations, rules, and refs |
| `throttle.is_disabled` | `True` if the throttle is currently disabled |
| `throttle.disable()` | Disable this throttle (async, acquires update lock) |
| `throttle.enable()` | Re-enable this throttle (async, acquires update lock) |
| `throttle.update_rate(rate)` | Update rate (string / `Rate` / callable) atomically |
| `throttle.update_backend(b)` | Swap backend atomically |
| `throttle.update_strategy(s)` | Swap strategy atomically |
| `throttle.update_cost(c)` | Update cost (int / callable) atomically |
| `throttle.update_min_wait_period(ms)` | Set wait floor (ms) atomically |
| `throttle.update_handle_throttled(h)` | Swap throttled-response handler atomically |
| `throttle.update_headers(h)` | Replace header collection atomically |
| `throttle.update_identifier(fn)` | Replace identifier function atomically |

--8<-- "includes/abbreviations.md"
