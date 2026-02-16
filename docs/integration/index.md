# Integration

Traffik doesn't force you into one pattern, use whichever fits your architecture.

Every throttle is a plain Python object. You can attach it to a route via FastAPI's dependency injection, wrap a function with a decorator, drop it into ASGI middleware, or call it directly inside your handler logic. All four approaches use the same underlying throttle instance, they're just different entry points.

---

## The four integration methods

### [Dependency Injection](dependencies.md)

Use `Depends(throttle)` in your route signature or router-level dependencies. FastAPI resolves the throttle automatically on every request. No `Request` parameter required in most cases.

This is the **most idiomatic approach for FastAPI** and works well for per-route and per-router throttling.

```python
@app.get("/items", dependencies=[Depends(throttle)])
async def list_items():
    ...
```

[Read more about dependency injection](dependencies.md)

---

### [Decorators](decorators.md)

Apply `@throttled(...)` directly to a route function. Two variants exist: one for Starlette (needs a `Request` parameter in the function), and one for FastAPI (no `Request` needed, FastAPI's DI handles it).

Decorators keep the throttle configuration visible at the handler level without modifying the route signature.

```python
@app.get("/items")
@throttled(throttle)
async def list_items():
    ...
```

[Read more about the decorator approach](decorators.md)

---

### [Middleware](middleware.md)

Mount `ThrottleMiddleware` once and apply throttles globally or to specific paths, methods, or request predicates. Ideal for cross-cutting concerns you want enforced before any route handler runs.

```python
app.add_middleware(
    ThrottleMiddleware,
    middleware_throttles=[MiddlewareThrottle(throttle, path="/api/")],
)
```

[Read more about middleware](middleware.md)

---

### [Direct Usage](direct-usage.md)

Call the throttle methods yourself inside a handler. Use `await throttle.hit(request)` to consume quota, `await throttle.check(request)` for a non-consuming pre-check, and `await throttle.stat(request)` to read the current state.

This gives you full programmatic control for dynamic cost calculation, multi-step workflows, or conditional throttling logic.

```python
@app.post("/upload")
async def upload(request: Request):
    cost = calculate_upload_cost(request)
    await throttle.hit(request, cost=cost)
    ...
```

[Read more about direct usage](direct-usage.md)

---

## Choosing an approach

| Approach | Best for |
|---|---|
| Dependency injection | Per-route and per-router throttling in FastAPI |
| Decorators | Keeping throttle config co-located with the handler |
| Middleware | Global or path-based throttling across all routes |
| Direct usage | Dynamic costs, conditional logic, multi-step workflows |

There is no wrong choice. Dependency injection and decorators are interchangeable stylistic preferences. Middleware and direct usage solve different structural problems. You can also combine them, for example, a global middleware throttle plus a tighter per-route dependency.


