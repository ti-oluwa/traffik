"""
HTTP dependency-mode benchmark target: a single throttled `GET /test`,
via `Depends(throttle)` - the most common Traffik integration pattern.

Run directly for manual poking:

    BENCH_RATE=100/60s uvicorn benchmarks.apps.http:app --port 8000

Also reused, with `BENCH_BACKEND=multiprocess`, as the target for the
`multiprocess` benchmark command - its scenarios exercise the same
`Depends`-based `/test` endpoint, just under gunicorn with multiple
forked workers sharing one backend instance.
"""

from fastapi import Depends, FastAPI, Request

from benchmarks.apps.config import backend_from_env, env, strategy_from_env
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle

backend = backend_from_env()
strategy = strategy_from_env()
registry = ThrottleRegistry()

throttle = HTTPThrottle(
    uid=env("BENCH_UID", "bench_http"),
    rate=env("BENCH_RATE", "100/60s"),
    backend=backend,
    strategy=strategy,
    registry=registry,
    on_error=env("BENCH_ON_ERROR", "raise"),  # type: ignore[arg-type]
)

app = FastAPI(lifespan=backend.lifespan)


@app.get("/test")
async def test_endpoint(request: Request = Depends(throttle)):
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    await backend.reset()
    return {"status": "reset"}
