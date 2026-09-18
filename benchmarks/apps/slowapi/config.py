"""Shared config helpers for the SlowAPI benchmark apps."""

from fastapi import Request
from limits.storage.base import Storage
from slowapi.util import get_remote_address

from benchmarks.env import get_env, int_env

BACKEND = get_env("BENCH_BACKEND", "inmemory").lower()
NAMESPACE = get_env("BENCH_NAMESPACE", "bench")

# traffik strategy name -> limits strategy name. Only strategies both
# libraries actually implement the same way belong here.
STRATEGY_MAP = {
    "fixed_window": "fixed-window",
    "sliding_window_counter": "sliding-window-counter",
}

UNSUPPORTED_BACKEND_MESSAGE = (
    "No SlowAPI-comparable storage for backend {backend!r}. `compare` "
    "supports inmemory, aioredis, coredis, aiomcache, and emcache."
)
UNSUPPORTED_STRATEGY_MESSAGE = (
    "No SlowAPI-comparable strategy for {strategy!r}. `compare` supports: "
    + ", ".join(STRATEGY_MAP)
)


def storage_uri_from_env() -> str:
    """Map `BENCH_BACKEND` onto the matching `limits` storage URI."""
    if BACKEND == "inmemory":
        return "memory://"
    if BACKEND in ("aioredis", "coredis"):
        return get_env("BENCH_REDIS_URL", "redis://localhost:6379/0")
    if BACKEND in ("aiomcache", "emcache"):
        host = get_env("BENCH_MEMCACHED_HOST", "localhost")
        port = int_env("BENCH_MEMCACHED_PORT", 11211)
        return f"memcached://{host}:{port}"
    raise ValueError(UNSUPPORTED_BACKEND_MESSAGE.format(backend=BACKEND))


def strategy_from_env() -> str:
    """Map `BENCH_STRATEGY` onto the matching `limits` strategy name."""
    kind = get_env("BENCH_STRATEGY", "fixed_window").lower()
    if kind not in STRATEGY_MAP:
        raise ValueError(UNSUPPORTED_STRATEGY_MESSAGE.format(strategy=kind))
    return STRATEGY_MAP[kind]


def get_identifier(request: Request) -> str:
    """Same identity rule as the traffik side: `X-Client-ID`, else peer IP."""
    client_id = request.headers.get("X-Client-ID")
    if client_id:
        return client_id
    return get_remote_address(request)


def reset_storage(storage: Storage) -> None:
    """
    Clear all limiter state.

    `MemcachedStorage.reset()` raises `NotImplementedError` (real
    Memcached has no scan-by-prefix), so fall back to the underlying
    pymemcache client's `flush_all()` - the same workaround traffik's own
    memcached backends use for `clear()`.
    """
    try:
        storage.reset()
    except NotImplementedError:
        storage.storage.flush_all()  # type: ignore[attr-defined]
