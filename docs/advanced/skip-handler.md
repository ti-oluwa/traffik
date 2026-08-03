# Skip Handler

Sometimes you want a request to be marked as throttled without letting the throttle immediately take over the response flow. This is the purpose of `skip_handler`.

It is useful when you want the throttle to keep its bookkeeping and wait state, but you want your app to decide what happens next. This is especially nice for API routes that need to inspect `is_throttled()` or `get_wait()` and then decide whether to raise a custom error, return a different payload, or simply log the event and continue.

`skip_handler` is available on:

- `HTTPThrottle` / `WebSocketThrottle`
- `QuotaContext.consume(..., skip_handler=...)`
- `MiddlewareThrottle(..., skip_handler=...)`
- `ThrottleMiddleware(..., skip_handler=...)`

With `skip_handler=True`, the connection is still marked throttled, the strategy state is still updated, and the wait period is still computed. The difference is that `handle_throttled` is never invoked and no automatic exception/response is raised.

---

## Why this exists

The default throttle behavior is convenient: if a client is over limit, Traffik calls your `handle_throttled` hook and lets the formal throttling flow take over. That is great for simple apps.

But sometimes you want to keep the rate-limit logic while taking control yourself. For example:

- Return a custom JSON body instead of a bare 429
- Apply special handling only to some clients or tenants
- Do a second layer of policy enforcement after the throttle is already marked
- Inspect the wait state and decide whether to proceed or short-circuit

This is where `skip_handler` comes in.

---

## On a throttle

```python
from fastapi import FastAPI, Request, HTTPException
from traffik import HTTPThrottle
from traffik.throttles import get_wait, is_throttled

app = FastAPI()

throttle = HTTPThrottle(
    "api:reports",
    rate="10/min",
    skip_handler=True,
)


@app.get("/reports")
async def get_reports(request: Request):
    await throttle(request)
    if is_throttled(request):
        wait_ms = get_wait(request)
        raise HTTPException(429, f"Try again in {wait_ms}ms")
    return {"ok": True}
```

This pattern is useful when you want to preserve the request's throttle state, but still let your own route logic decide the final response.

### Per-hit override

You can also override the throttle-level default on a single call:

```python
await throttle(request, skip_handler=True)
```

This is handy when only one request path or one specific branch should bypass the handler while the rest of the app stays at the default behavior.

---

## Using `get_wait()` and `is_throttled()`

When `skip_handler` is enabled, the public status checks still work exactly as usual:

```python
from traffik.throttles import get_wait, is_throttled

wait_ms = await throttle(request, skip_handler=True)

if is_throttled(request):   # you could also check `wait_ms > 0`
    assert wait_ms == get_wait(request)
    # decide what to do next
```

This is the key part: the throttle still records the wait period, but it no longer forces the handler path.

!!! tip
    If you are using `skip_handler=True`, you likely want to check `get_wait()` yourself, although the throttle will still return the `wait_ms` too, which you can use instead, and decide how to respond. That is the whole point of the feature.

---

## In quota context

`QuotaContext` supports the same idea when you want to queue quota usage without triggering the throttle callback immediately.

```python
from fastapi import FastAPI, Request
from traffik import HTTPThrottle
from traffik.throttles import get_wait, is_throttled

app = FastAPI()

throttle = HTTPThrottle("api:batch", rate="5/min")


@app.post("/batch")
async def new_batch(request: Request):
    async with throttle.quota(request) as quota:
        quota.consume(cost=2, skip_handler=True)

        if is_throttled(request):
            wait_ms = get_wait(request)
            return {
                "message": "quota queued, but the client is already throttled",
                "retry_after_ms": wait_ms,
            }

        result = await do_work()

    return result
```

This is a nice fit when you need to mark the connection as throttled, but still let your own app logic decide whether the request should continue, retry, or return a custom error.

---

## In middleware

With `MiddlewareThrottle`, the same pattern is available without having to touch each route.

```python
from fastapi import FastAPI
from traffik import HTTPThrottle
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware

app = FastAPI()

throttle = HTTPThrottle("api:global", rate="100/min")

app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[
        MiddlewareThrottle(throttle, path="/api/", skip_handler=True),
    ],
)
```

The middleware still records the throttle state and leaves the request marked as throttled, but it does not invoke the normal `handle_throttled` path. The route is still free to inspect the request state and decide how to answer.

### At the middleware level

`ThrottleMiddleware` itself also accepts `skip_handler`:

```python
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[MiddlewareThrottle(throttle, path="/api/")],
    skip_handler=True,
)
```

This is useful when you want the whole middleware stack to behave the same way: mark the request as throttled, keep the state, but let your downstream app decide what to do next.

!!! warning
    This is intentionally different from the default middleware behavior. By default, `ThrottleMiddleware` will raise or handle a throttle as soon as it detects a rate limit. With `skip_handler=True`, the request is passed through to the app as if it were not blocked, so you must check `is_throttled()` or `get_wait()` yourself downstream if you need to act on it.

---

## Good fit vs bad fit

`skip_handler` is a good choice when you want:

- custom response bodies
- app-layer enforcement after the throttle is already known
- side effects that should not be triggered by the default throttled handler
- a request that should be tracked as throttled but still reach your route logic

It is not a good fit when you want the throttle to completely own the response flow. In that case, leave `skip_handler` alone and let the built-in handler do its thing.

The rule of thumb is simple: if you want the app to decide the response, use `skip_handler=True`.

---

## Summary

`skip_handler` lets Traffik behave like a rate-limit state tracker without hijacking the response flow. It keeps the stat, marks the connection as throttled, and exposes `get_wait()` / `is_throttled()` for your own app logic.

That gives you the best of both worlds:

- the backend still enforces the limit
- the request still carries throttle state
- the app still decides what the client sees

And because it is supported in throttles, quota contexts, and middleware, it fits cleanly into both route-level and cross-cutting rate limiting patterns.
