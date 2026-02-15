# Quota Context (Deferred Throttling)

This is one of Traffik's most powerful features, and also the one most people don't need on day one. But when you do need it, you'll be very glad it exists.

---

## The Problem

Standard throttling is optimistic: consume quota first, then do the work. Most of the time that's fine. But imagine this scenario:

1. Client sends a request to generate a report (costs 10 quota)
2. Traffik immediately deducts 10 from the quota counter
3. The report generation fails halfway through
4. The quota is gone, but the client got nothing

Or the reverse:

1. You want to check three different throttles before doing expensive work
2. If *any* of them would reject the request, you don't want the others consumed either
3. With standard throttling you'd have to hit each throttle, track which ones fired, and manually undo... which you can't

**Quota Context** solves this. It queues throttle hits and only actually consumes them when you say so — or not at all if something goes wrong.

!!! warning "This is an advanced feature"
    Quota Context adds real complexity. For most use cases, standard `Depends(throttle)` is simpler and should be preferred. Use Quota Context when you have specific conditional consumption requirements.

---

## Bound Mode

The most common usage: create a context tied to one throttle.

```python
from fastapi import FastAPI, Request, Depends
from traffik import HTTPThrottle
from traffik.backends.redis import RedisBackend

backend = RedisBackend("redis://localhost:6379", namespace="myapp")
app = FastAPI(lifespan=backend.lifespan)

throttle = HTTPThrottle("api:reports", rate="50/hour", backend=backend)


@app.post("/reports/generate")
async def generate_report(request: Request):
    # Queue throttle hits — they are NOT consumed yet
    async with throttle.quota(request) as quota:
        await quota(cost=10)        # Queued: cost=10
        await quota(cost=5)         # Aggregated! Still one entry: cost=15

        # Do the expensive work
        report = await generate_expensive_report()

    # Context exits successfully -> quota consumed (15 units deducted)
    return {"report": report}
```

If `generate_expensive_report()` raises an exception, the context exits with an error — and **no quota is consumed**. The client doesn't pay for a failed operation.

---

## Unbound Mode

When you need to control multiple throttles together, use `QuotaContext` directly:

```python
from traffik.quotas import QuotaContext

burst_throttle = HTTPThrottle("api:burst", rate="20/min", backend=backend)
daily_throttle = HTTPThrottle("api:daily", rate="500/day", backend=backend)


@app.post("/exports")
async def create_export(request: Request):
    async with QuotaContext(request) as quota:
        # Must specify throttle explicitly in unbound mode
        await quota(burst_throttle, cost=5)
        await quota(daily_throttle, cost=5)

        result = await run_export()

    return {"export": result}
```

Both throttles are consumed atomically on successful exit, or neither is consumed on failure.

---

## Cost Aggregation

Consecutive calls with the same throttle and identical configuration are automatically merged into a single backend operation. This is a performance optimization — fewer round-trips to the backend.

```python
async with throttle.quota(request) as quota:
    await quota(cost=2)          # Entry 1: cost=2
    await quota(cost=3)          # Aggregated into Entry 1: cost=5
    await quota()                # Aggregated into Entry 1: cost=6
    await quota(other_throttle)  # Entry 2: different throttle, new entry
    await quota(cost=1)          # Entry 1 again? No — other_throttle broke the streak
                                 # This becomes Entry 3
```

Aggregation breaks when: different throttle, different context, different retry config, or a cost function is used (can't know the cost until apply time).

---

## Conditional Consumption

### apply_on_error

Control whether quota is consumed when an exception occurs:

```python
# Default: don't consume on any exception
async with throttle.quota(request, apply_on_error=False) as quota:
    await quota(cost=5)
    result = await risky_operation()  # Exception -> no quota consumed

# Always consume, even on exceptions (use sparingly)
async with throttle.quota(request, apply_on_error=True) as quota:
    await quota(cost=5)
    await risky_operation()  # Exception -> quota still consumed

# Consume only for specific exception types
from fastapi import HTTPException
async with throttle.quota(request, apply_on_error=(ValueError,)) as quota:
    await quota(cost=5)
    await risky_operation()
    # ValueError -> consume quota
    # Any other exception -> don't consume quota
```

### apply_on_exit=False

Disable automatic consumption on exit and manage it yourself:

```python
async with throttle.quota(request, apply_on_exit=False) as quota:
    await quota(cost=5)

    is_valid = await validate_business_rules()
    if not is_valid:
        await quota.cancel()  # Discard — no quota consumed
        return {"error": "validation_failed"}

    result = await process()
    await quota.apply()  # Manually consume quota after success

return {"result": result}
```

### Manual apply() and cancel()

```python
async with throttle.quota(request, apply_on_exit=False) as quota:
    await quota(cost=10)

    try:
        result = await complex_operation()
        await quota.apply()   # Success: consume
    except ExpectedError:
        await quota.cancel()  # Known failure: don't consume
        raise
```

`apply()` is idempotent — calling it twice is safe. `cancel()` is final — you cannot un-cancel a context.

---

## Pre-checking with check()

Sometimes you want to check if quota *would* be available before committing to an operation:

```python
# Check at the throttle level
if not await throttle.check(request, cost=10):
    raise HTTPException(429, "Insufficient quota for this operation")

# Check inside a quota context
async with throttle.quota(request) as quota:
    await quota(cost=10)

    if not await quota.check():  # Check using the owner throttle
        await quota.cancel()
        raise HTTPException(429, "Rate limit would be exceeded")

    result = await expensive_operation()
```

!!! warning "TOCTOU caveat"
    `check()` is a best-effort snapshot. Between the check and the actual consumption, another request from the same client could use up the remaining quota. For strong guarantees, use `lock=True` (see below) or design your system to handle `ConnectionThrottled` exceptions gracefully.

---

## Nested Contexts

Child contexts merge their queued entries into the parent when they exit successfully:

```python
async with throttle.quota(request) as parent:
    await parent(cost=2)

    async with parent.nested() as child:
        await child(cost=1)
        # child exits successfully -> merges cost=1 into parent's queue

    # Parent's queue now has cost=3 total
    await parent(cost=1)  # Aggregated: still same entry if config matches

# All consumed here: 4 total units
```

The parent acquires the lock (if configured). Child contexts under a parent don't acquire their own lock by default — they operate under the parent's lock context.

---

## Locking

Enable locking to make the entire quota context atomic with respect to other contexts using the same key:

```python
# Use throttle UID as lock key (simplest)
async with throttle.quota(request, lock=True) as quota:
    await quota(cost=5)
    result = await process()  # Lock held for entire duration — keep this fast!

# Custom lock key
async with throttle.quota(request, lock="user:123:api_calls") as quota:
    await quota(cost=5)

# Custom lock config
async with throttle.quota(
    request,
    lock=True,
    lock_config={"ttl": 30, "blocking_timeout": 5}
) as quota:
    await quota(cost=5)
```

!!! warning "Keep locked contexts fast"
    The lock is held for the entire duration of the `async with` block. Long-running operations (database queries, external API calls) inside a locked context will block other requests waiting for the same lock, increasing latency significantly.

---

## Retry with Backoff

Individual quota entries can be configured to retry on failure:

```python
from traffik.backoff import ExponentialBackoff

async with throttle.quota(request) as quota:
    await quota(
        cost=5,
        retry=3,                           # Up to 3 retries
        backoff=ExponentialBackoff(multiplier=2.0, max_delay=10.0),
        base_delay=0.5,                    # Start with 0.5s delay
    )
    await quota(
        cost=1,
        retry=2,
        retry_on=(TimeoutError,),          # Only retry on timeouts
    )
```

### Backoff Strategies

| Strategy | Behavior | Best For |
|---|---|---|
| `ConstantBackoff` | Same delay every retry | Simple retry logic, predictable timing |
| `LinearBackoff(increment=1.0)` | Delay grows by `increment` each attempt | Gradual back-pressure |
| `ExponentialBackoff(multiplier=2.0)` | Delay doubles each attempt, with optional jitter | Production retries, thundering-herd prevention |
| `LogarithmicBackoff(base=2.0)` | Delay grows logarithmically | Many retries with diminishing returns |

The default backoff when `retry > 0` is `ExponentialBackoff(multiplier=2.0, max_delay=60.0, jitter=True)`.

---

## Inspecting the Context

You can inspect a quota context's state at any time:

```python
async with throttle.quota(request) as quota:
    await quota(cost=5)
    await quota(cost=3)

    print(quota.queued_cost)    # 8 (estimated, excludes cost functions)
    print(quota.applied_cost)   # 0 (nothing consumed yet)
    print(quota.active)         # True (not consumed or cancelled)
    print(quota.consumed)       # False
    print(quota.cancelled)      # False
    print(quota.is_bound)       # True (created via throttle.quota())
    print(quota.is_nested)      # False
    print(quota.depth)          # 0
```

After the context exits:

```python
print(quota.applied_cost)   # 8
print(quota.consumed)       # True
print(quota.active)         # False
```

---

## Limitations

Be aware of these before reaching for `QuotaContext`:

| Limitation | Detail |
|---|---|
| No rollback on partial failure | If entry 3 of 5 fails during `apply()`, entries 1 and 2 are already consumed |
| TOCTOU with `check()` | Quota can change between `check()` and `apply()` — use locks for strong consistency |
| `cancelled` is final | Once cancelled, a context cannot be un-cancelled or re-used |
| `apply()` is idempotent | Calling it multiple times only consumes once — safe but not a retry mechanism |
| Nested lock ordering | Acquiring locks in different orders in nested contexts can deadlock |
| Cost functions resolved at apply | If your throttle uses a cost function, `queued_cost` is an estimate |
