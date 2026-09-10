# Advanced Features

If you are here, you should already know the basics: setup your backend, create a throttle, attach it to a route, done. That gets you surprisingly far, but Traffik has a deeper toolkit for the situations where the "one size fits all" doesn't quite fit.

This section is for when you're ready to graduate from `"100/min"` to something that actually mirrors how your API works in the real world.

---

## What's in Here

### [Request Costs](request-costs.md)

Not every request deserves the same weight on the rate limit scale. A health check is a feather. A file upload is a boulder. Request costs let you reflect that asymmetry directly in how quota is consumed.

```python
# A bulk export endpoint that counts as 10 requests worth of quota
throttle = HTTPThrottle("export", rate="100/min", cost=10)
```

[Learn about request costs &rarr;](request-costs.md)

---

### [Multiple Rate Limits](multiple-limits.md)

Production APIs rarely need just one limit. You need a strict burst cap *and* a generous hourly envelope. Stack throttles to enforce both simultaneously, with a clear failure model.

```python
burst  = HTTPThrottle("api:burst", rate="20/min")
hourly = HTTPThrottle("api:sustained", rate="500/hour")

# Both must pass. First failure wins
@app.get("/api/data", dependencies=[Depends(burst), Depends(hourly)])
async def get_data():
    ...
```

[Learn about layered throttling &rarr;](multiple-limits.md)

---

### [Throttle Rules & Wildcards](rules.md)

A throttle applies globally by default - every request that hits the router it's attached to. Rules let it ask "do I even apply to this connection?" first, before touching the backend or computing an identifier, so one throttle can target a specific path, method, or condition instead of everything.

```python
from traffik.registry import Rule

# Only throttle GET requests to /api/users
rule = Rule(path="/api/users", methods={"GET"})
throttle = HTTPThrottle("api:users", rate="500/min", rules={rule})
```

[Learn about rules &rarr;](rules.md)

---

### [Exemptions](exemptions.md)

Some clients should never be throttled, such as your internal services, premium users, admin tokens, and whitelisted IPs. The `EXEMPTED` sentinel lets you carve out those exceptions cleanly, with zero overhead.

```python
from traffik import EXEMPTED

async def tiered_identifier(request: Request):
    if is_admin_token(request.headers.get("x-admin-token")):
        return EXEMPTED       # Admin? Go right through.
    return request.client.host  # Everyone else: throttled by IP.
```

[Learn about exemptions &rarr;](exemptions.md)

---

### [Context-Aware Backends](context-backends.md)

Building a multi-tenant SaaS? Different tenants likely need different backends. Enterprise tenants get their own Redis instance, free-tier users share one pool. The `dynamic_backend=True` flag makes the throttle resolve which backend to use
on every request instead of locking in one backend at startup.

```python
throttle = HTTPThrottle(
    "api:quota",
    rate="1000/hour",
    dynamic_backend=True,   # Resolved per-request from context
)
```

[Learn about context-aware backends &rarr;](context-backends.md)

---

### [Response Headers](headers.md)

A `429` with no explanation leaves clients flying blind - no idea how many requests they have left, when the window resets, or when it's safe to retry. Traffik computes standard rate-limit header values (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`) for you to attach to a response however you see fit; it doesn't inject them automatically.

```python
from traffik import DEFAULT_HEADERS_ALWAYS, DEFAULT_HEADERS_THROTTLED

throttle = HTTPThrottle("api:data", rate="100/min", headers=DEFAULT_HEADERS_ALWAYS)
```

[Learn about response headers &rarr;](headers.md)

---

### [Custom Throttled Handlers](throttled-handlers.md)

The default 429-or-JSON-message behavior is fine for most apps, but not all. Maybe you want an exact `Retry-After` timestamp, a specific WebSocket message shape, or a response body that matches the rest of your API's error format. A throttled handler replaces the default entirely.

```python
async def handler(connection, wait_ms, throttle, context):
    return JSONResponse({"error": "slow down", "retry_in_ms": wait_ms}, status_code=429)

throttle = HTTPThrottle("api:data", rate="100/min", handle_throttled=handler)
```

[Learn about custom throttled handlers &rarr;](throttled-handlers.md)

---

### [Skip Handler](skip-handler.md)

Normally, a throttled request never reaches your route - the handler takes over and that's the end of it. `skip_handler=True` turns that off: state still updates and `wait_ms` still gets computed exactly as normal, but your own code decides what response to send, instead of the handler.

```python
throttle = HTTPThrottle("api:reports", rate="10/min", skip_handler=True)

@app.get("/reports")
async def get_reports(request: Request):
    await throttle(request)
    if is_throttled(request):
        return cached_report()  # degrade instead of failing outright
    return generate_report()
```

[Learn about skip_handler &rarr;](skip-handler.md)

---

### [Strategy Statistics](statistics.md)

Sometimes you want to look at a rate limit counter without touching it - for a `X-RateLimit-Remaining` header, a `/usage` endpoint, or feeding a metrics system. `throttle.stat(...)` reads the current state from the backend and never consumes quota.

```python
stat = await throttle.stat(request, context={...})
```

[Learn about statistics &rarr;](statistics.md)

---

### [Quota Context (Deferred Throttling)](quota-context.md)

Standard throttling is optimistic: quota is consumed first, work happens after. That's wrong when the work might fail (don't want to charge quota for nothing) or when several throttles need to agree before anything is consumed at all. `QuotaContext` defers consumption until you explicitly commit it.

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle

throttle = HTTPThrottle("api:reports", rate="50/hour")

@app.post("/reports/generate")
async def generate_report(request: Request):
    async with throttle.quota(request) as ctx:
        report = await do_expensive_work()  # only consume quota if this succeeds
        await ctx.apply()
    return report
```

[Learn about quota context &rarr;](quota-context.md)

---

### [Throttle Registry](registry.md)

Every throttle belongs to a `ThrottleRegistry` - the coordination layer that tracks which throttles are active, holds the rules that gate them, and lets you disable or re-enable throttles at runtime (a maintenance mode switch, a feature flag) without touching route code.

```python
from traffik.registry import ThrottleRegistry

registry = ThrottleRegistry()
registry.disable_all()  # e.g. during a maintenance window
```

[Learn about the registry &rarr;](registry.md)

---

## When Do You Need These?

| You want to... | Feature to use |
|---|---|
| Charge more quota for expensive operations | [Request Costs](request-costs.md) |
| Enforce burst + sustained limits together | [Multiple Rate Limits](multiple-limits.md) |
| Target a throttle at specific paths, methods, or conditions | [Rules](rules.md) |
| Let admins or premium users bypass throttling | [Exemptions](exemptions.md) |
| Give each tenant isolated rate limit counters | [Context-Aware Backends](context-backends.md) |
| Tell clients how many requests they have left | [Response Headers](headers.md) |
| Customize what happens when a client is throttled | [Custom Throttled Handlers](throttled-handlers.md) |
| Let your own code decide the response, not the default handler | [Skip Handler](skip-handler.md) |
| Read rate limit state without consuming quota | [Statistics](statistics.md) |
| Only consume quota if the work actually succeeds | [Quota Context](quota-context.md) |
| Disable or re-enable throttles at runtime | [Throttle Registry](registry.md) |

!!! tip "You can combine all of these"
    These features compose neatly. A dynamic-backend throttle can have per-request
    costs and an identifier that returns `EXEMPTED` for admin tokens. Stack them as
    your use case demands.
