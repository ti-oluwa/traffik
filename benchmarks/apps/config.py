"""
Reads `BENCH_*` environment variables into the pieces a target app module
needs: a throttle backend, a strategy, and the throttle's own settings.
"""

import os
import typing

from starlette.requests import Request

from benchmarks.backends import STRATEGIES
from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.memcached.aiomcache import MemcachedBackend as AiomcacheBackend
from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.backends.redis.aioredis import RedisBackend as AioredisBackend
from traffik.backends.redis.coredis import RedisBackend as CoredisBackend


def env(name: str, default: str) -> str:
    """Read a environment variable, falling back to `default`."""
    return os.environ.get(name, default)


def int_env(name: str, default: int) -> int:
    """Read a environment variable as an int."""
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


async def get_identifier(connection: Request) -> str:
    """
    Benchmark connection identifier: `X-Client-ID` header, falling back to
    the real peer address (this is a real socket now, so this is a real IP).
    """
    client_id = connection.headers.get("X-Client-ID")
    if client_id:
        return client_id
    return connection.client[0] if connection.client else "anonymous"


def backend_from_env() -> ThrottleBackend[typing.Any, typing.Any]:
    """
    Build the throttle backend selected by `BENCH_BACKEND`.

    For `multiprocess`, this also calls `.start()` immediately - safe and
    required to happen here, at module-import time, so that gunicorn's
    `--preload` (which imports the app module once in its master before
    forking) does the shared-memory setup exactly once, before any worker
    exists, matching `MultiProcessInMemoryBackend`'s documented deployment
    pattern.
    """
    kind = env("BENCH_BACKEND", "inmemory").lower()
    # Unique per server process (the port is unique per run) so repeated
    # benchmark invocations never collide on namespace/shared-memory names.
    namespace = env("BENCH_NAMESPACE", "bench")

    if kind == "inmemory":
        return InMemoryBackend(
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
            number_of_shards=int_env("BENCH_SHARDS", 32),
        )
    elif kind == "multiprocess":
        backend = MultiProcessInMemoryBackend(
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
            number_of_shards=int_env("BENCH_SHARDS", 32),
            max_keys=int_env("BENCH_MP_MAX_KEYS", 65536),
            cleanup_frequency=30.0,
        )
        backend.start()
        return backend
    elif kind == "aioredis":
        return AioredisBackend(
            connection=env("BENCH_REDIS_URL", "redis://localhost:6379/0"),
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
        )
    elif kind == "coredis":
        return CoredisBackend(
            connection=env("BENCH_REDIS_URL", "redis://localhost:6379/0"),
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
        )
    elif kind == "aiomcache":
        return AiomcacheBackend(
            host=env("BENCH_MEMCACHED_HOST", "localhost"),
            port=int_env("BENCH_MEMCACHED_PORT", 11211),
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
            track_keys=True,
        )
    elif kind == "emcache":
        from traffik.backends.memcached.emcache import (
            MemcachedBackend as EmcacheBackend,
        )

        return EmcacheBackend(
            host=env("BENCH_MEMCACHED_HOST", "localhost"),
            port=int_env("BENCH_MEMCACHED_PORT", 11211),
            namespace=namespace,
            identifier=get_identifier,
            persistent=False,
            track_keys=True,
        )
    else:
        raise ValueError(f"Unknown `BENCH_BACKEND`: {kind!r}")


def strategy_from_env():
    """Build the throttling strategy selected by `BENCH_STRATEGY`."""
    kind = env("BENCH_STRATEGY", "fixed_window").lower()
    if kind not in STRATEGIES:
        raise ValueError(f"Unknown `BENCH_STRATEGY`: {kind!r}")
    return STRATEGIES[kind]()
