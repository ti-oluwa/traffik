# Advanced Features

You know the basics: create a throttle, attach it to a route, done. That gets you
surprisingly far, but Traffik has a deeper toolkit for the situations where "one
size fits all" doesn't quite fit.

This section is for when you're ready to graduate from `"100/min"` to something
that actually mirrors how your API works in the real world.

---

## What's in Here

### [Request Costs](request-costs.md)

Not every request deserves the same weight on the rate limit scale. A health check
is a feather. A file upload is a boulder. Request costs let you reflect that
asymmetry directly in how quota is consumed.

```python
# A bulk export endpoint that counts as 10 requests worth of quota
throttle = HTTPThrottle("export", rate="100/min", cost=10)
```

[Learn about request costs &rarr;](request-costs.md)

---

### [Multiple Rate Limits](multiple-limits.md)

Production APIs rarely need just one limit. You need a strict burst cap *and* a
generous hourly envelope. Stack throttles to enforce both simultaneously, with a
clear failure model.

```python
burst  = HTTPThrottle("api:burst",     rate="20/min")
hourly = HTTPThrottle("api:sustained", rate="500/hour")

# Both must pass — first failure wins
@app.get("/api/data", dependencies=[Depends(burst), Depends(hourly)])
async def get_data():
    ...
```

[Learn about layered throttling &rarr;](multiple-limits.md)

---

### [Exemptions](exemptions.md)

Some clients should never be throttled, such as your internal services, premium users,
admin tokens, and whitelisted IPs. The `EXEMPTED` sentinel lets you carve out those
exceptions cleanly, with zero overhead: no counter touched, no backend called.

```python
from traffik import EXEMPTED

async def tiered_identifier(request: Request):
    if request.headers.get("x-admin-token") == ADMIN_TOKEN:
        return EXEMPTED       # Admin? Go right through.
    return request.client.host  # Everyone else: throttled by IP.
```

[Learn about exemptions &rarr;](exemptions.md)

---

### [Context-Aware Backends](context-backends.md)

Building a multi-tenant SaaS? Different tenants likely need different backends.
Enterprise tenants get their own Redis instance, free-tier users share one pool.
The `dynamic_backend=True` flag makes the throttle resolve which backend to use
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

## When Do You Need These?

| You want to... | Feature to use |
|---|---|
| Charge more quota for expensive operations | [Request Costs](request-costs.md) |
| Enforce burst + sustained limits together | [Multiple Rate Limits](multiple-limits.md) |
| Let admins or premium users bypass throttling | [Exemptions](exemptions.md) |
| Give each tenant isolated rate limit counters | [Context-Aware Backends](context-backends.md) |

!!! tip "You can combine all of these"
    These features compose cleanly. A dynamic-backend throttle can have per-request
    costs and an identifier that returns `EXEMPTED` for admin tokens. Stack them as
    your use case demands.


