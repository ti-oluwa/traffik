# Request Costs

Here's a scenario: your `/upload` endpoint lets users push files up to 100 MB.
Your `/health` endpoint returns `{"status": "ok"}` in microseconds. Should they
both tick the rate limit counter by exactly one?

Probably not.

Request costs are Traffik's answer to this. Every `hit(...)` consumes a configurable
amount of quota instead of a flat `1`. You get to decide what "expensive" means for
your API.

---

## The Default: Cost of 1

By default, every request costs 1 unit of quota. A throttle set to `"100/min"` allows
100 requests per minute. This is fine for most endpoints.

```python
from traffik import HTTPThrottle

# 100 requests per minute, each costs 1 (the default)
throttle = HTTPThrottle(uid="api:default", rate="100/min")
```

---

## Fixed Cost at Initialization

Pass `cost=N` when you create the throttle to make every request through it consume
`N` units of quota. This is the simplest way to make an endpoint feel "heavier" to
the rate limiter.

```python
from traffik import HTTPThrottle
from fastapi import Depends

# Each request to this throttle burns 10 units — same as 10 normal requests
export_throttle = HTTPThrottle(
    uid="api:export",
    rate="100/min",
    cost=10,
)

@app.get("/export", dependencies=[Depends(export_throttle)])
async def export_data():
    ...
```

!!! tip "Thinking in units"
    A throttle with `rate="100/min"` and `cost=10` effectively allows 10 export
    requests per minute. The math is: `rate.limit / cost = effective_limit`.

---

## Dynamic Cost via Function

When the cost depends on the request itself — file size, number of records, operation
type — pass an async function instead of an integer. Traffik calls it on every hit,
passing the connection and the current context.

```python
from traffik import HTTPThrottle
from starlette.requests import Request
import typing

async def upload_cost(
    request: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> int:
    # Cost = file size in megabytes, minimum 1
    content_length = int(request.headers.get("content-length", 0))
    size_mb = content_length // (1024 * 1024)
    return max(size_mb, 1)

upload_throttle = HTTPThrottle(
    uid="api:upload",
    rate="500/hour",  # 500 MB of uploads per user per hour
    cost=upload_cost,
)
```

The cost function signature is:

```python
async def my_cost(
    connection: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> int:
    ...
```

Both parameters are provided automatically by Traffik. `context` will be `None`
unless you passed a `context` dict when initializing the throttle or calling `hit(...)`.

---

## Per-Call Override

Sometimes you know the cost only at the moment of the call — for example, after
parsing a request body. Pass `cost=N` directly to `hit(...)` or `__call__(...)` to
override the throttle's default for that one request.

```python
@app.post("/process")
async def process(request: Request):
    body = await request.json()
    operation = body.get("operation", "read")

    # Charge more quota for mutations
    cost_map = {"read": 1, "write": 5, "delete": 10}
    cost = cost_map.get(operation, 1)

    await throttle(request, cost=cost)
    return {"status": "processed"}
```

!!! note "Per-call cost takes priority"
    When you pass `cost=N` to `hit(...)`, it overrides both the fixed `cost` set at
    initialization and any dynamic cost function. Think of it as the last word.

---

## Cost of Zero: The Exemption Shortcut

A cost of `0` is special: Traffik sees it and short-circuits immediately — no counter
is incremented, no backend is called. The request passes through as if it was never
throttled.

```python
async def selective_cost(
    request: Request,
    context: typing.Optional[typing.Dict[str, typing.Any]],
) -> int:
    # Health checks and metrics scrapes don't count against the limit
    if request.url.path in {"/health", "/metrics", "/readyz"}:
        return 0
    return 1
```

!!! warning "Cost 0 vs `EXEMPTED`"
    Returning `0` from a cost function and returning `EXEMPTED` from an identifier
    function both skip throttling — but they do it at different stages:

    - **`cost=0`**: Skips *before* the identifier is resolved (immediately `hit(...)` is called).
    - **`EXEMPTED`**: Skips *at the identifier stage*. Nothing downstream runs at all.

    For exempting entire client categories (admins, internal services), `EXEMPTED`
    from the identifier is cleaner.
    For skipping specific paths or methods within a shared throttle, `cost=0` is
    a pragmatic shortcut and slightly cheaper.

---

## Real-World Examples

=== "File Upload (Size-Based)"

    Charge quota proportional to the uploaded file's size. A 50 MB upload consumes
    50x the quota of a 1 MB upload.

    ```python
    from traffik import HTTPThrottle
    from starlette.requests import Request
    import typing

    async def file_size_cost(
        request: Request,
        context: typing.Optional[typing.Dict[str, typing.Any]],
    ) -> int:
        content_length = int(request.headers.get("content-length", 0))
        # Cost = ceil(bytes / 1 MB), minimum 1
        size_mb = max(1, -(-content_length // (1024 * 1024)))  # ceiling division
        return size_mb

    upload_throttle = HTTPThrottle(
        uid="uploads:per-user",
        rate="500/hour",   # 500 MB of uploads per user per hour
        cost=file_size_cost,
        identifier=get_user_id,
    )
    ```

=== "AI Token Counting"

    Charge quota based on how many tokens an AI request consumed, reported back by
    the model. Use `hit(...)` manually after the response so you know the actual count.

    ```python
    from traffik import HTTPThrottle
    from fastapi import Request

    token_throttle = HTTPThrottle(
        uid="ai:token-budget",
        rate="100000/day",   # 100k tokens per user per day
        identifier=get_user_id,
    )

    @app.post("/ai/complete")
    async def complete(request: Request):
        body = await request.json()

        # Call the model first — we need the actual token count
        result = await call_llm(body["prompt"])
        tokens_used = result["usage"]["total_tokens"]

        # Now consume that many units of quota
        await token_throttle(request, cost=tokens_used)

        return result
    ```

=== "Operation Type (Read / Write / Delete)"

    Different operations have different blast radii. A DELETE that wipes a table
    should cost more than a GET that reads one row.

    ```python
    from traffik import HTTPThrottle
    from starlette.requests import Request
    import typing

    OPERATION_COSTS = {
        "GET":    1,
        "POST":   3,
        "PUT":    3,
        "PATCH":  3,
        "DELETE": 10,
    }

    async def method_cost(
        request: Request,
        context: typing.Optional[typing.Dict[str, typing.Any]],
    ) -> int:
        method = request.method.upper()
        return OPERATION_COSTS.get(method, 1)

    api_throttle = HTTPThrottle(
        uid="api:weighted",
        rate="100/min",
        cost=method_cost,
    )

    # With this setup:
    # - 100 GET requests per minute (100 × 1 = 100 units)
    # - 33 POST/PUT/PATCH requests per minute (33 × 3 ≈ 100 units)
    # - 10 DELETE requests per minute (10 × 10 = 100 units)
    ```

=== "Bulk Operations"

    Endpoints that operate on multiple records at once should count each record,
    not each HTTP request.

    ```python
    from traffik import HTTPThrottle
    from fastapi import Request

    async def bulk_cost(
        request: Request,
        context: typing.Optional[typing.Dict[str, typing.Any]],
    ) -> int:
        body = await request.json()
        items = body.get("items", [])
        # Each item in the batch counts as one unit, minimum 1
        return max(len(items), 1)

    bulk_throttle = HTTPThrottle(
        uid="api:bulk-write",
        rate="1000/hour",
        cost=bulk_cost,
    )
    ```

---

## Summary

| Approach | When to use |
|---|---|
| `cost=N` (integer) | All requests to this throttle are equally expensive |
| `cost=async_fn` | Cost depends on the request content or headers |
| `await throttle(request, cost=N)` | You know the cost only at call time |
| `cost=0` | Skip throttling for certain requests within a shared throttle |
