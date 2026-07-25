"""
Middleware-mode benchmark target: `ThrottleMiddleware` applied to `/test`,
with `/unthrottled` left exempt for the selective-throttling scenario.

    BENCH_RATE=100/60s uvicorn benchmarks.apps.middleware:app --port 8000
"""

from fastapi import FastAPI

from benchmarks.apps.config import backend_from_env, env, strategy_from_env
from traffik.middleware import MiddlewareThrottle, ThrottleMiddleware
from traffik.registry import ThrottleRegistry
from traffik.throttles import HTTPThrottle

backend = backend_from_env()
strategy = strategy_from_env()
registry = ThrottleRegistry()

throttle = HTTPThrottle(
    uid=env("BENCH_UID", "bench_middleware"),
    rate=env("BENCH_RATE", "100/60s"),
    backend=backend,
    strategy=strategy,
    registry=registry,
    on_error=env("BENCH_ON_ERROR", "raise"),  # type: ignore[arg-type]
)
middleware_throttle = MiddlewareThrottle(throttle, path="/test", methods={"GET"})

app = FastAPI(lifespan=backend.lifespan)
app.add_middleware(  # type: ignore[arg-type]
    ThrottleMiddleware,
    middleware_throttles=[middleware_throttle],  # type: ignore[arg-type]
)


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
    await backend.reset()
    return {"status": "reset"}
