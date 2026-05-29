import sys
import typing

from starlette.requests import Request

from benchmarks.base import BenchmarkConfig
from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.backends.memcached.aiomcache import MemcachedBackend as AiomcacheBackend
from traffik.backends.multiprocess import MultiProcessInMemoryBackend
from traffik.backends.redis.aioredis import RedisBackend as AioredisRedisBackend
from traffik.backends.redis.coredis import RedisBackend as CoredisRedisBackend
from traffik.strategies.custom import GCRAStrategy
from traffik.strategies.fixed_window import FixedWindowStrategy
from traffik.strategies.leaky_bucket import (
    LeakyBucketStrategy,
    LeakyBucketWithQueueStrategy,
)
from traffik.strategies.sliding_window import (
    SlidingWindowCounterStrategy,
    SlidingWindowLogStrategy,
)
from traffik.strategies.token_bucket import (
    TokenBucketStrategy,
    TokenBucketWithDebtStrategy,
)

HAS_EMCACHE: bool = False

if sys.platform != "win32" or sys.platform != "cygwin":
    try:
        from traffik.backends.memcached.emcache import (
            MemcachedBackend as EmcacheBackend,
        )

        HAS_EMCACHE = True
    except ImportError:
        HAS_EMCACHE = False


STRATEGY_MAP: typing.Dict[str, typing.Any] = {
    "fixed_window": FixedWindowStrategy,
    "sliding_window_counter": SlidingWindowCounterStrategy,
    "sliding_window_log": SlidingWindowLogStrategy,
    "token_bucket": TokenBucketStrategy,
    "token_bucket_debt": TokenBucketWithDebtStrategy,
    "leaky_bucket": LeakyBucketStrategy,
    "leaky_bucket_queue": LeakyBucketWithQueueStrategy,
    "gcra": GCRAStrategy,
}


async def get_identifier(connection: Request) -> str:
    """
    Default benchmark identifier: uses X-Client-ID header or falls back to IP.

    :param connection: The HTTP connection.
    :return: A string identifier for the connection.
    """
    client_id = connection.headers.get("X-Client-ID")
    if client_id:
        return client_id

    # Try to get remote address
    return connection.client[0] if connection.client else "anonymous"


def create_backend(config: BenchmarkConfig) -> ThrottleBackend:
    """
    Instantiate the backend specified by config.backend_kind.

    :param config: Benchmark configuration.
    :return: An uninitialized backend instance.
    :raises ValueError: If backend_kind is unknown or unavailable on this platform.
    """
    backend_kind = config.backend_kind.lower()

    if backend_kind == "inmemory":
        return InMemoryBackend(
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
            number_of_shards=8,
        )
    elif backend_kind == "multiprocess":
        return MultiProcessInMemoryBackend(
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
            number_of_shards=config.multiprocess_shards,
            max_keys=config.multiprocess_max_keys,
        )
    elif backend_kind == "redis_aioredis":
        return AioredisRedisBackend(
            connection=config.redis_url,
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
        )
    elif backend_kind == "redis_coredis":
        return CoredisRedisBackend(
            connection=config.redis_url,
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
        )
    elif backend_kind == "memcached_aiomcache":
        return AiomcacheBackend(
            host=config.memcached_host,
            port=config.memcached_port,
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
        )
    elif backend_kind == "memcached_emcache":
        if not HAS_EMCACHE:
            raise ValueError(
                f"Backend '{backend_kind}' is not available on this platform. "
                "EmCache requires a POSIX system."
            )
        return EmcacheBackend(  # type: ignore
            host=config.memcached_host,
            port=config.memcached_port,
            namespace="bench",
            identifier=get_identifier,
            persistent=False,
        )
    else:
        raise ValueError(f"Unknown backend kind: {backend_kind}")


def create_strategy(config: BenchmarkConfig):
    """
    Instantiate the strategy specified by config.strategy_kind.

    :param config: Benchmark configuration.
    :return: An instantiated strategy object.
    :raises ValueError: If strategy_kind is unknown.
    """
    strategy_kind = config.strategy_kind.lower()

    if strategy_kind not in STRATEGY_MAP:
        raise ValueError(f"Unknown strategy kind: {strategy_kind}")

    strategy_class = STRATEGY_MAP[strategy_kind]
    return strategy_class()
