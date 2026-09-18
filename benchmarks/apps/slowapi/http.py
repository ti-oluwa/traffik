"""
SlowAPI-equivalent benchmark target, for the `compare` command.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.slowapi.http:app --port 8001
"""

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from benchmarks.apps.slowapi.config import (
    NAMESPACE,
    get_identifier,
    reset_storage,
    storage_uri_from_env,
    strategy_from_env,
)
from benchmarks.env import get_env
from benchmarks.rates import traffik_rate_to_limits_string

limiter = Limiter(
    key_func=get_identifier,
    storage_uri=storage_uri_from_env(),
    strategy=strategy_from_env(),
    key_prefix=NAMESPACE,
    swallow_errors=(get_env("BENCH_ON_ERROR", "raise").lower() == "allow"),
)

RATE = traffik_rate_to_limits_string(get_env("BENCH_RATE", "100/60s"))

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


@app.get("/test")
@limiter.limit(RATE)
async def test_endpoint(request: Request):
    return {"status": "ok"}


# Sync def: see benchmarks/apps/traffik/http.py's test_endpoint_sync.
@app.get("/test-sync")
@limiter.limit(RATE)
def test_endpoint_sync(request: Request):
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    reset_storage(limiter._storage)  # type: ignore[arg-type]
    return {"status": "reset"}
