# Shorthand Specs

A **shorthand spec** is a short string that describes a throttle, so you can write the limit right where it applies instead of building a `Throttle` object first.

```python
from fastapi import FastAPI
from traffik.backends.inmemory import InMemoryBackend
from traffik.decorators.fastapi import throttled

backend = InMemoryBackend(namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

@app.get("/items")
@throttled("api:items|100/min")  # <-- the whole throttle, in one string
async def list_items():
    return {"items": []}
```

That is equivalent to creating `HTTPThrottle(uid="api:items", rate="100/min")` and passing it to `@throttled(...)`, without the extra variable and import.

!!! note "Where specs work"
    Shorthand specs are accepted by the `@throttled(...)` decorator (both the FastAPI and the Starlette/generic variants). Dependencies (`Depends(...)`), `MiddlewareThrottle` and direct `throttle.hit(...)` calls still take a `Throttle` instance. See [Decorators](decorators.md) for the two decorator variants.

---

## The format

A spec is up to four segments, separated by `|`:

```
uid | rate | strategy | type
```

Every segment but the rate is optional. The `uid` can be left out too, but read [Always give shared throttles a uid](#always-give-shared-throttles-a-uid) before you do.

| Segment | What it is | Default |
|---|---|---|
| `uid` | Unique id of the throttle, used to namespace its state in the backend. Can contain `:` freely (`"api:items"`). | Generated (random) |
| `rate` | Any [rate string](../core-concepts/rates.md): `100/min`, `10/30s`, `200 per 500ms`. | Required |
| `strategy` | One of the aliases in the table below. | `fixed_window` |
| `type` | `http` or `ws`. | `http` |

### Strategy aliases

| Alias | Strategy |
|---|---|
| `fixed_window` | `FixedWindow` |
| `sliding_window_counter` | `SlidingWindowCounter` |
| `sliding_window_log` | `SlidingWindowLog` |
| `token_bucket` | `TokenBucket` |
| `leaky_bucket` | `LeakyBucket` |
| `leaky_bucket_with_queue` | `LeakyBucketWithQueue` |
| `gcra` | `GCRA` |

Strategies without an alias (`TokenBucketWithDebt` and everything in `traffik.strategies.custom`) need a `Throttle` built directly.

---

## Examples

| Spec | What you get |
|---|---|
| `"api:items\|100/min"` | 100 requests per minute, throttle named `api:items`, fixed window |
| `"api:search\|10/s\|token_bucket"` | 10 per second with bursts allowed, token bucket |
| `"api:feed\|5/min\|sliding_window_counter"` | 5 per minute with no boundary bursts |
| `"api:upload\|2/10s\|gcra"` | 2 per 10 seconds, evenly spaced |
| `"chat:connect\|30/min\|fixed_window\|ws"` | A `WebSocketThrottle` guarding the handshake, 30 connections per minute |
| `"api:items"` | No rate: reuse the throttle already registered as `api:items` |
| `"100/min"` | Rate only: a generated uid, fixed window |

### Stack several limits

Pass several specs, or mix specs and `Throttle` objects. They are checked in order, and the first one to reject the request stops the rest.

```python
@app.get("/search")
@throttled("search:burst|10/s", "search:sustained|200/min")
async def search(q: str):
    return {"query": q, "results": []}
```

```python
sustained = HTTPThrottle(uid="search:sustained", rate="200/min", strategy=SlidingWindowCounter())

@app.get("/search")
@throttled("search:burst|10/s", sustained)  # a spec and a pre-built throttle together
async def search(q: str):
    ...
```

### WebSockets

Add `ws` as the type segment. This guards the connection (handshake), the same as using a `WebSocketThrottle` with `@throttled`.

```python
@app.websocket("/ws")
@throttled("chat:connect|30/min|fixed_window|ws")
async def chat(websocket: WebSocket):
    await websocket.accept()
    ...
```

When every spec in the call is a WebSocket spec, you can set the type once with a keyword instead: `@throttled("chat:connect|30/min", type="ws")`. For per-message throttling inside an open connection, see [Direct Usage](direct-usage.md).

### Starlette

The Starlette variant takes exactly the same strings. The route must declare a `Request` (or `WebSocket`) parameter, as always with that variant.

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
from traffik.decorators.generic import throttled

@throttled("api:items|100/min|token_bucket")
async def list_items(request: Request) -> JSONResponse:
    return JSONResponse({"items": []})
```

### Reuse a throttle by uid

A spec with only a uid does not create anything. It looks the uid up in the [registry](../advanced/registry.md) and uses what is registered, which is a clean way to apply one limit to several routes:

```python
report_throttle = HTTPThrottle(uid="api:report", rate="1/min")  # keep a reference!

@app.get("/report/daily")
@throttled("api:report")
async def daily_report(): ...

@app.get("/report/weekly")
@throttled("api:report")
async def weekly_report(): ...
```

!!! warning "Keep a reference to the throttle you look up"
    The registry holds throttles weakly. A throttle that you create and never assign to a name can be garbage collected, and the lookup then fails with a `ParseError`. Assign it to a variable (or an attribute) that lives as long as your app.

---

## Settings shared by every spec

Keyword arguments on `@throttled(...)` are applied to each spec that builds a new throttle. This is how you set anything the string cannot express, such as the identifier or the backend, without repeating it:

```python
async def user_id(connection) -> str:
    return connection.headers.get("x-user-id", "anonymous")

@app.get("/me")
@throttled("api:me|60/min", "api:me:daily|1000/day", identifier=user_id)
async def me(): ...
```

| Keyword | Effect |
|---|---|
| `uid="..."` | uid for a spec that does not embed one. Only allowed when exactly one spec in the call needs it. |
| `type="http"` or `"ws"` | Type for specs that do not embed one. Must not conflict with a type a spec does embed. |
| anything else a throttle accepts | `identifier`, `backend`, `cost`, `headers`, `on_error`, and so on. Applied to every spec that creates a throttle. Not applied to pre-built `Throttle` objects or to uid-only lookups (those reject keywords other than `registry`). |

---

## Always give shared throttles a uid

A spec without a uid (`"100/min"`) gets a random one **every time the module is imported**. That is fine for a single process, but each worker process generates its own uid, and the uid is part of the key in the backend:

- With `InMemoryBackend` there is nothing shared anyway.
- With **Redis, Memcached or the multi-process backend and more than one worker**, each worker counts against its own key, so the limit is multiplied by the number of workers.

For anything that runs in more than one process, embed a stable uid: `"api:items|100/min"`, not `"100/min"`.

---

## What it cannot express

A spec is deliberately small. When you need any of the following, build the `Throttle` directly:

- a strategy without an alias (custom strategies, `TokenBucketWithDebt`)
- options that belong to one throttle type, such as HTTP's `use_method`
- a rate that is computed at request time

Mixing is always fine: pre-built throttles and specs can share a single `@throttled(...)` call.

---

## Errors you will see

Specs are parsed when the decorator is applied, which is at import time, so mistakes surface when the app starts and not on the first request. All of them raise `traffik.exceptions.ParseError`.

| Spec | Problem |
|---|---|
| `""` | Empty spec |
| `"api\|"` | Empty segment (stray `\|`) |
| `"api\|fast"` | The second segment must be a rate when the first is a uid |
| `"100/min\|nope"` | Unknown strategy. The message lists the supported ones |
| `"100/min\|token_bucket\|tcp"` | Unknown type (use `http` or `ws`) |
| `"100/min\|token_bucket\|ws\|x"` | Too many segments |
| `"api:missing"` | No throttle is registered under that uid |
