"""SlowAPI HTTP target used by the Traffik comparison benchmark."""

import re

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


def get_identifier(request: Request) -> str:
    """Use the same benchmark identity header as the Traffik target."""
    return request.headers.get("X-Client-ID") or get_remote_address(request)


def to_slowapi_rate(rate: str) -> str:
    """Convert Traffik's ``count/seconds`` notation to a limits rate string."""
    match = re.fullmatch(r"(\d+)/(\d+)s", rate)
    if not match:
        raise ValueError(f"Unsupported benchmark rate: {rate!r}")
    count, seconds = match.groups()
    return f"{count} per {seconds} second"


RATE = to_slowapi_rate(__import__("os").getenv("BENCH_RATE", "100/60s"))

limiter = Limiter(key_func=get_identifier, storage_uri="memory://")
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/test")
@limiter.limit(RATE)
async def test_endpoint(request: Request):
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    limiter.reset()
    return {"status": "reset"}
