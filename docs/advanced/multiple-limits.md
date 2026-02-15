# Multiple Rate Limits (Layered Throttling)

A single rate limit is rarely enough for a production API.

A user who sends 20 requests in the first second and then waits 59 seconds hasn't
violated a "100/minute" rule — but they've hammered your service with a burst that
your infrastructure may not appreciate. Conversely, a steady drumbeat of 1 request
every second looks fine per-minute but might add up to a problematic volume per hour.

Layered throttling lets you enforce both at once: a tight short-term cap and a
generous long-term envelope. The user must stay within *all* limits simultaneously.

---

## How It Works

Think of it like a two-tier system:

- **Burst limit**: Protects your service from sudden traffic spikes. Short window, tight cap.
- **Sustained limit**: Ensures fair long-term use. Long window, generous cap.

A request only succeeds if it passes *every* throttle in the chain. The first
throttle to reject it wins — subsequent throttles in the chain are not evaluated.

---

## Stacking with `Depends`

The most straightforward approach for FastAPI: list your throttles as dependencies.
FastAPI resolves them in order.

```python
from fastapi import Depends, FastAPI
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")

# Tier 1: No more than 10 requests per minute (burst protection)
burst_throttle = HTTPThrottle(
    uid="api:burst",
    rate="10/min",
    backend=backend,
)

# Tier 2: No more than 100 requests per hour (sustained fairness)
sustained_throttle = HTTPThrottle(
    uid="api:sustained",
    rate="100/hour",
    backend=backend,
)

app = FastAPI()

@app.get(
    "/api/data",
    dependencies=[Depends(burst_throttle), Depends(sustained_throttle)],
)
async def get_data():
    return {"data": "..."}
```

A client who hits this endpoint is subject to both rules simultaneously. They can
make at most 10 requests per minute, and at most 100 over the course of an hour.

---

## Stacking with `@throttled`

The `@throttled` decorator from `traffik.decorators` is the FastAPI-native way to
apply multiple throttles as a route decorator. Pass all throttles in a single call.

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.decorators import throttled  # FastAPI-specific decorator

burst_throttle     = HTTPThrottle(uid="api:burst",     rate="10/min")
sustained_throttle = HTTPThrottle(uid="api:sustained", rate="100/hour")

app = FastAPI()

@app.get("/api/data")
@throttled(burst_throttle, sustained_throttle)
async def get_data():
    return {"data": "..."}
```

!!! note "Import the right `throttled`"
    There are two `throttled` functions in Traffik:

    - `traffik.decorators.throttled` — designed for **FastAPI**, works with its
      dependency injection system. Routes do not need an explicit connection parameter.
    - `traffik.throttles.throttled` — the **Starlette** version. The decorated route
      must have a `Request` or `WebSocket` parameter.

    For FastAPI, always use `from traffik.decorators import throttled`.

---

## Stacking with `Depends` on a Router

For router-wide limits, add the throttles to the router's `dependencies` list.
Every route on the router inherits them.

```python
from fastapi import APIRouter, Depends
from traffik import HTTPThrottle

burst_throttle     = HTTPThrottle(uid="v1:burst",     rate="10/min")
sustained_throttle = HTTPThrottle(uid="v1:sustained", rate="100/hour")

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(burst_throttle), Depends(sustained_throttle)],
)

@router.get("/users")
async def get_users():
    ...

@router.get("/orgs")
async def get_orgs():
    ...
```

---

## Order Matters

Throttles are checked sequentially in the order you pass them. **The first failure
stops the chain.** No subsequent throttles run.

This has practical implications for efficiency and user experience:

```python
# Good: burst check first — cheaper (shorter window, fewer stored keys)
#       and more informative (the user knows they burst, not that they
#       exhausted their hourly budget)
dependencies=[Depends(burst_throttle), Depends(sustained_throttle)]

# Also valid: sustained check first — useful if you want to enforce
#             the hourly cap as the primary signal
dependencies=[Depends(sustained_throttle), Depends(burst_throttle)]
```

!!! tip "Recommend: strictest limit first"
    Put your tightest limit first. It fails fast and saves you a backend roundtrip
    for the looser limit. It also gives the client a more actionable `Retry-After`
    header — "wait 30 seconds" is more useful than "wait 54 minutes".

---

## Real-World Example: API with Three Tiers

A realistic public API often needs three tiers: per-second, per-minute, and
per-day.

```python
from fastapi import Depends, FastAPI
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")

# 1. Per-second burst cap: no firehosing
per_second = HTTPThrottle(
    uid="api:per-second",
    rate="5/s",
    backend=backend,
)

# 2. Per-minute envelope: sustained usage
per_minute = HTTPThrottle(
    uid="api:per-minute",
    rate="60/min",
    backend=backend,
)

# 3. Daily quota: long-term fair use
per_day = HTTPThrottle(
    uid="api:per-day",
    rate="5000/day",
    backend=backend,
)

app = FastAPI()

@app.get(
    "/api/data",
    dependencies=[
        Depends(per_second),   # Strictest first
        Depends(per_minute),
        Depends(per_day),      # Loosest last
    ],
)
async def get_data():
    return {"data": "..."}
```

A well-behaved client using this endpoint can sustain 5 requests per second,
as long as they don't exceed 60 per minute, and don't blow their 5000-per-day
quota.

---

## Starlette Example

If you're using Starlette directly (not FastAPI), use `@throttled` from `traffik.throttles`.
Your route must accept a `Request` parameter — the decorator finds it automatically.

```python
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from traffik import HTTPThrottle
from traffik.throttles import throttled  # Starlette version

burst_throttle     = HTTPThrottle(uid="star:burst",     rate="10/min")
sustained_throttle = HTTPThrottle(uid="star:sustained", rate="100/hour")

@throttled(burst_throttle, sustained_throttle)
async def get_data(request: Request):
    return JSONResponse({"data": "..."})

app = Starlette(routes=[Route("/api/data", get_data)])
```

---

## Quick Reference

| Pattern | Use case |
|---|---|
| `Depends(a), Depends(b)` | Route-level, explicit, most readable |
| `@throttled(a, b)` | FastAPI decorator style, same result |
| Router `dependencies=[...]` | Apply to every route in a router |
| Multiple `@throttled` decorators | Not supported — pass all throttles in one call |

!!! warning "Each throttle has its own counter"
    Every `HTTPThrottle` instance tracks usage independently by its `uid`. Make sure
    each throttle you create has a unique `uid` — Traffik raises a `ConfigurationError`
    if you try to register two throttles with the same UID.
