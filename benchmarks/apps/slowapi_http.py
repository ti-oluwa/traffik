"""
SlowAPI-equivalent benchmark target, for the `compare` command.

Run directly for manual poking:

    BENCH_RATE=100/60s uvicorn benchmarks.apps.slowapi_http:app --port 8001
"""

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from benchmarks.apps.config import get_env, int_env
from benchmarks.rates import traffik_rate_to_limits_string

BACKEND = get_env("BENCH_BACKEND", "inmemory").lower()
NAMESPACE = get_env("BENCH_NAMESPACE", "bench")

UNSUPPORTED_BACKEND_MESSAGE = (
    "No SlowAPI-comparable storage for backend {backend!r}. `compare` "
    "supports inmemory, aioredis, coredis, aiomcache, and emcache - not "
    "multiprocess, which has no fork-safe equivalent in SlowAPI's storage "
    "backends. See this module's docstring for why."
)


def storage_uri_from_env() -> str:
    """Map a `BENCH_BACKEND` value onto the matching `limits` storage URI."""
    if BACKEND == "inmemory":
        return "memory://"
    if BACKEND in ("aioredis", "coredis"):
        # Same URL as the traffik side - literally the same Redis.
        return get_env("BENCH_REDIS_URL", "redis://localhost:6379/0")
    if BACKEND in ("aiomcache", "emcache"):
        host = get_env("BENCH_MEMCACHED_HOST", "localhost")
        port = int_env("BENCH_MEMCACHED_PORT", 11211)
        return f"memcached://{host}:{port}"
    raise ValueError(UNSUPPORTED_BACKEND_MESSAGE.format(backend=BACKEND))


def get_identifier(request: Request) -> str:
    """Same identity rule as `benchmarks.apps.config.get_identifier`."""
    client_id = request.headers.get("X-Client-ID")
    if client_id:
        return client_id
    return get_remote_address(request)


limiter = Limiter(
    key_func=get_identifier,
    storage_uri=storage_uri_from_env(),
    strategy="fixed-window",
    key_prefix=NAMESPACE,
    swallow_errors=(get_env("BENCH_ON_ERROR", "raise").lower() == "allow"),
)

RATE = traffik_rate_to_limits_string(get_env("BENCH_RATE", "100/60s"))

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/test")
@limiter.limit(RATE)
async def test_endpoint(request: Request):
    return {"status": "ok"}


@app.get("/test-sync")
@limiter.limit(RATE)
def test_endpoint_sync(request: Request):
    return {"status": "ok"}


@app.get("/__bench__/health")
async def health():
    return {"status": "ok"}


@app.post("/__bench__/reset")
async def reset():
    limiter._storage.reset()
    return {"status": "reset"}
