"""
Traffik HTTP mode benchmark targets.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.traffik.http:app --port 8000
"""

from fastapi import Depends, FastAPI, Request

from benchmarks.apps.traffik.config import backend_from_env, get_env, strategy_from_env
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle

backend = backend_from_env()
strategy = strategy_from_env()
registry = ThrottleRegistry()

throttle = HTTPThrottle(
    uid=get_env("BENCH_UID", "bench_http"),
    rate=get_env("BENCH_RATE", "100/60s"),
    backend=backend,
    strategy=strategy,
    registry=registry,
    on_error=get_env("BENCH_ON_ERROR", "raise"),  # type: ignore[arg-type]
)

app = FastAPI(lifespan=backend.lifespan)


@app.get("/test")
async def test_endpoint(request: Request = Depends(throttle)):  # noqa: B008
    return {"status": "ok"}


@app.get("/test-sync")
def test_endpoint_sync(request: Request = Depends(throttle)):  # noqa: B008
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    await backend.reset()
    return {"status": "reset"}
