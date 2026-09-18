"""
Middleware-mode SlowAPI-equivalent, for `compare --mode middleware`.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.slowapi.middleware:app --port 8001
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from limits import parse
from limits.storage import storage_from_string
from limits.strategies import STRATEGIES

from benchmarks.apps.slowapi.config import (
    get_identifier,
    reset_storage,
    storage_uri_from_env,
    strategy_from_env,
)
from benchmarks.env import get_env
from benchmarks.rates import traffik_rate_to_limits_string

storage = storage_from_string(storage_uri_from_env())
limiter = STRATEGIES[strategy_from_env()](storage)
rate_item = parse(traffik_rate_to_limits_string(get_env("BENCH_RATE", "100/60s")))

app = FastAPI()


@app.middleware("http")
async def throttle_middleware(request: Request, call_next):
    if request.url.path != "/test":
        return await call_next(request)
    if not limiter.hit(rate_item, get_identifier(request)):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
    return await call_next(request)


@app.get("/test")
async def test_endpoint():
    return {"status": "ok"}


@app.get("/unthrottled")
async def unthrottled_endpoint():
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    reset_storage(storage)  # type: ignore[arg-type]
    return {"status": "reset"}
