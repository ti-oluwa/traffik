# Skip Handler

By default, a throttled request never really reaches your code - `handle_throttled` takes over, raises or responds, and that's the end of the story. `skip_handler` turns that off: the throttle still does everything *except* hand control to the handler. State updates, the connection gets marked throttled, `wait_ms` gets computed - all of it happens exactly as normal. The only thing that doesn't happen is the handler call. Your code gets there and decides for itself.

`skip_handler` is available everywhere a throttle actually runs:

- `HTTPThrottle` / `WebSocketThrottle` - at construction, or per-call
- `QuotaContext.consume(..., skip_handler=...)`
- `MiddlewareThrottle(..., skip_handler=...)`
- `ThrottleMiddleware(..., skip_handler=...)` - applies to every throttle in the stack

---

## Why you'd want this

The default flow (handler owns the response) is right for most endpoints. Reach for `skip_handler` when the response needs to depend on something the handler can't know about - a few real cases:

- **Degrade instead of fail.** A throttled request doesn't have to mean a 429 - it could mean "serve the cached version instead."
- **Per-client policy on top of the throttle.** Free-tier users get a 429; paying users on the same throttle get a queued response.
- **A second check before deciding anything.** You want to know the request is throttled *and* check something else (a feature flag, a maintenance window) before picking a response.
- **Log-and-continue.** You want throttled requests recorded for observability, but they shouldn't actually block anything (a soft-launch/dry-run limit).

If none of that applies and a plain 429 is genuinely fine, you don't need this. The default handler already does the right thing with less code.

---

## On a throttle

The pattern is always: call the throttle, then check `is_throttled()`/`get_wait()` yourself. `await throttle(request)` returns the connection, not the wait time, so if you want the number, you need to ask for it separately, or use whichever form of the return value is convenient:

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle, get_wait, is_throttled

app = FastAPI()

reports_throttle = HTTPThrottle("api:reports", rate="10/min", skip_handler=True)

@app.get("/reports")
async def get_reports(request: Request):
    await reports_throttle(request)

    if is_throttled(request):
        # Still counted against the limit, still has a real wait time -
        # we're just choosing to serve something instead of a bare 429.
        cached = await get_cached_report()
        if cached is not None:
            return {"data": cached, "stale": True, "retry_after_ms": get_wait(request)}
        raise HTTPException(429, f"Try again in {get_wait(request)}ms")

    return {"data": await generate_fresh_report()}
```

Nothing about the throttle's bookkeeping changes here. `reports_throttle` still enforces `10/min` exactly as it would without `skip_handler`. The only difference is that *your* code, not `handle_throttled`, decides what the client actually sees.

### Per-call override

The throttle-level `skip_handler` is just a default. You can override it per call when only specific paths through your code should behave differently:

```python
# Constructed with the normal default (skip_handler=False)
throttle = HTTPThrottle("api:reports", rate="10/min")

@app.get("/reports")
async def get_reports(request: Request):
    # ...standard path, handler runs as usual on throttle...
    await throttle(request)
    return {"data": await generate_fresh_report()}

@app.get("/reports/preview")
async def get_reports_preview(request: Request):
    # This one route wants to degrade instead of 429ing.
    await throttle(request, skip_handler=True)
    if is_throttled(request):
        return {"data": await get_cached_report(), "stale": True}
    return {"data": await generate_fresh_report()}
```

Both routes share the same `10/min` budget on `throttle`. The only difference is how each one *reacts* to being throttled.

---

## In a quota context

`QuotaContext.consume()` is sync (it queues the hit rather than immediately executing it, so there's nothing to await until the context exits) - `skip_handler` works the same way there:

```python
from fastapi import FastAPI, HTTPException, Request
from traffik import HTTPThrottle, get_wait, is_throttled

app = FastAPI()
batch_throttle = HTTPThrottle("api:batch", rate="5/min")

@app.post("/batch")
async def new_batch(request: Request):
    async with batch_throttle.quota(request) as quota:
        quota.consume(cost=2, skip_handler=True)

        if is_throttled(request):
            return {
                "message": "queued - you're currently rate limited",
                "retry_after_ms": get_wait(request),
            }

        result = await do_work()

    return result
```

This is the same idea as the plain-throttle case, just inside a context that's already batching multiple `consume()` calls together. One thing worth knowing: if you mix `skip_handler=True` and `skip_handler=False` calls in the same quota context, Traffik won't merge them into one batched hit. The entries only combine when every setting, `skip_handler` included, actually matches. Keep the same `skip_handler` value across a context if you want your consecutive `consume()` calls to batch as one backend round trip.

---

## In middleware

Same feature, applied without touching individual routes.

```python
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(throttle, path="/api/", skip_handler=True),
    ],
)
```

Or apply it to the whole stack at once rather than per-throttle:

```python
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[MiddlewareThrottle(throttle, path="/api/")],
    skip_handler=True,  # overrides every throttle's own setting in this middleware
)
```

!!! warning "A throttled request reaches your route either way"
    This is the one place `skip_handler` behaves differently than it does on a bare throttle. `Middleware` has nowhere else to hand control to. There's no "app" underneath it to skip to except your actual route. So with `skip_handler=True`, a throttled request is passed straight through to `app`, indistinguishable from an unthrottled one unless your route explicitly checks `is_throttled(request)`. Skip that check and the throttle becomes a no-op in practice: still tracked internally, but nothing anywhere actually reacts to it.

---

## When to use it, and when not to

**Good fit**: custom response bodies, degrading gracefully instead of hard-failing, per-client policy on top of a shared limit, or anywhere you need to know a request was throttled without letting that fact alone decide the response.

**Not a good fit**: you just want a 429 (or your `handle_throttled` already does the right thing). `skip_handler` adds a manual `is_throttled()` check to every route for no benefit there. Leave it off and let the handler do its job.

---

## Summary

`skip_handler` separates "was this throttled" from "what should happen because of it." The throttle still enforces the limit and still updates every bit of state it normally would; you're only opting out of the automatic reaction. `is_throttled(connection)` and `get_wait(connection)` are the two functions doing the real work here. You call the throttle, then check them yourself. Available the same way on `HTTPThrottle`/`WebSocketThrottle`, `QuotaContext`, and both `MiddlewareThrottle` and `ThrottleMiddleware`, so it fits whether you're throttling one route or a whole app.
