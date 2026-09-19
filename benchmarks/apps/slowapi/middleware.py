"""
Middleware-mode SlowAPI-equivalent, for `compare --mode middleware`.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.slowapi.middleware:app --port 8001
"""

from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from benchmarks.apps.slowapi.config import (
    NAMESPACE,
    get_identifier,
    reset_storage,
    storage_uri_from_env,
    strategy_from_env,
)
from benchmarks.env import get_env
from benchmarks.rates import to_limits_rate

RATE = to_limits_rate(get_env("BENCH_RATE", "100/60s"))

limiter = Limiter(
    key_func=get_identifier,
    storage_uri=storage_uri_from_env(),
    strategy=strategy_from_env(),
    key_prefix=NAMESPACE,
    default_limits=[RATE],
    swallow_errors=(get_env("BENCH_ON_ERROR", "raise").lower() == "allow"),
)

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)


@app.get("/test")
async def test_endpoint():
    return {"status": "ok"}


@app.get("/unthrottled")
@limiter.exempt
async def unthrottled_endpoint():
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    reset_storage(limiter._storage)  # type: ignore[arg-type]
    return {"status": "reset"}
