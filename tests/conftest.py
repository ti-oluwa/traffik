"""Global test configuration and fixtures."""

import datetime
import logging
import multiprocessing
import os
import sys
import typing

import pytest
from starlette.requests import HTTPConnection

from tests.frameworks import ASGIFramework, FastAPIAdapter, StarletteAdapter
from tests.utils import HTTPConnectionT
from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend
from traffik.strategies import (
    custom,
    fixed_window,
    leaky_bucket,
    sliding_window,
    token_bucket,
)
from traffik.throttles import ThrottleStrategy

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
MEMCACHED_HOST = os.getenv("MEMCACHED_HOST", "localhost")
MEMCACHED_PORT = int(os.getenv("MEMCACHED_PORT", "11211"))
MEMCACHED_POOL_SIZE = int(os.getenv("MEMCACHED_POOL_SIZE", "2"))


class SkipBackend(Exception):
    """Raised to skip a backend test if the backend is not available."""


def get_inmemory_backend(namespace: str, persistent: bool) -> InMemoryBackend:
    return InMemoryBackend(
        persistent=persistent,
        namespace=namespace,
        number_of_shards=8,
        cleanup_frequency=2,
    )


def get_aioredis_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_REDIS_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")

    from traffik.backends.redis.aioredis import RedisBackend

    return RedisBackend(
        connection=REDIS_URL,
        namespace=namespace,
        persistent=persistent,
        lock_type="redis",
        url_kwargs={"max_connections": 500},
    )


def get_coredis_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_REDIS_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")

    from traffik.backends.redis.coredis import RedisBackend

    return RedisBackend(
        connection=REDIS_URL,
        namespace=namespace,
        persistent=persistent,
        url_kwargs={"max_connections": 500},
    )


def get_aiomcache_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_MEMCACHED_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend(
            "Skipping Memcached backend tests as per environment setting."
        )

    from traffik.backends.memcached.aiomcache import MemcachedBackend

    return MemcachedBackend(
        host=MEMCACHED_HOST,
        port=MEMCACHED_PORT,
        namespace=namespace,
        pool_size=MEMCACHED_POOL_SIZE,
        persistent=persistent,
        # Enable key tracking for testing purposes
        # So that we can clean up keys after tests
        track_keys=True,
        # To ensure that lock keys expires as we have way to track lock key and
        # expire/delete them, especailly when and error occurs acquisition
        lock_ttl=30,
    )


def get_emcache_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_MEMCACHED_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend(
            "Skipping Memcached backend tests as per environment setting."
        )

    from traffik.backends.memcached.emcache import MemcachedBackend

    return MemcachedBackend(
        host=MEMCACHED_HOST,
        port=MEMCACHED_PORT,
        namespace=namespace,
        persistent=persistent,
        # Enable key tracking for testing purposes
        # So that we can clean up keys after tests
        track_keys=True,
        # To ensure that lock keys expires as we have way to track lock key and
        # expire/delete them, especailly when and error occurs acquisition
        lock_ttl=30,
    )


def get_multiprocess_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    return MultiProcessInMemoryBackend(
        namespace=namespace,
        number_of_shards=8,
        max_keys=4096,
        cleanup_frequency=2.0,
        persistent=persistent,
    )


MAYBE_UNIX = sys.platform != "windows" and sys.platform != "cygwin"
BACKEND_FACTORIES: typing.List[
    typing.Callable[[str, bool], ThrottleBackend[typing.Any, typing.Any]]
] = [
    get_inmemory_backend,
    get_aiomcache_backend,
    get_aioredis_backend,
    get_coredis_backend,
]
if MAYBE_UNIX:
    supports_fork = "fork" in multiprocessing.get_all_start_methods()
    BACKEND_FACTORIES.append(get_emcache_backend)

    # if supports_fork:
    #     BACKEND_FACTORIES.append(get_multiprocess_backend)

    if supports_fork and multiprocessing.get_start_method(allow_none=True) != "fork":
        multiprocessing.set_start_method("fork", force=True)


class BackendGen(typing.Generic[HTTPConnectionT]):
    """Wraps a backend generator function to provide both iteration and call capabilities."""

    def __init__(
        self,
        func: typing.Callable[
            [str, bool], typing.Iterator[ThrottleBackend[typing.Any, HTTPConnectionT]]
        ],
    ) -> None:
        self._func = func

    def __iter__(self) -> typing.Iterator[ThrottleBackend[typing.Any, HTTPConnectionT]]:
        return iter(self())

    def __call__(
        self,
        *,
        namespace: str = "test",
        persistent: bool = False,
        exclude: typing.Optional[
            typing.Union[
                typing.Type[ThrottleBackend[typing.Any, typing.Any]],
                typing.Tuple[typing.Type[ThrottleBackend[typing.Any, typing.Any]], ...],
            ]
        ] = None,
    ) -> typing.Generator[ThrottleBackend[typing.Any, HTTPConnectionT], None, None]:
        for backend in self._func(namespace, persistent):
            if exclude is not None and isinstance(backend, exclude):
                continue
            yield backend


def _backend_gen(
    namespace: str = "test", persistent: bool = False
) -> typing.Generator[ThrottleBackend[typing.Any, HTTPConnection], None, None]:
    for backend_factory in BACKEND_FACTORIES:
        try:
            backend = backend_factory(namespace, persistent)
        except SkipBackend as exc:
            logger.debug(f"Skipping backend: {exc!s}")
            continue
        yield backend


@pytest.fixture(scope="function")
def backends() -> BackendGen[HTTPConnection]:
    """Provides a generator of throttle backends for testing"""
    return BackendGen(_backend_gen)


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def backend() -> typing.AsyncGenerator[InMemoryBackend, None]:
    """Provides a fresh instance of `InMemoryBackend` for each test, ensuring isolation and cleanup."""
    backend = InMemoryBackend()
    async with backend(persistent=False, close_on_exit=True):
        yield backend


STRATEGIES = [
    pytest.param(fixed_window.FixedWindowStrategy, id="fixed_window"),
    pytest.param(
        sliding_window.SlidingWindowCounterStrategy, id="sliding_window_counter"
    ),
    pytest.param(sliding_window.SlidingWindowLogStrategy, id="sliding_window_log"),
    pytest.param(token_bucket.TokenBucketStrategy, id="token_bucket"),
    pytest.param(token_bucket.TokenBucketWithDebtStrategy, id="token_bucket_with_debt"),
    pytest.param(leaky_bucket.LeakyBucketStrategy, id="leaky_bucket"),
    pytest.param(
        leaky_bucket.LeakyBucketWithQueueStrategy, id="leaky_bucket_with_queue"
    ),
]
"""Default set of throttle strategy types to test against."""

CUSTOM_STRATEGIES = [
    pytest.param(custom.TieredRateStrategy, id="tiered_rate"),
    pytest.param(custom.GCRAStrategy, id="gcra"),
    pytest.param(custom.AdaptiveThrottleStrategy, id="adaptive"),
    pytest.param(custom.PriorityQueueStrategy, id="priority_queue"),
    pytest.param(custom.QuotaWithRolloverStrategy, id="quota_with_rollover"),
    pytest.param(custom.TimeOfDayStrategy, id="time_of_day"),
    pytest.param(custom.CostBasedTokenBucketStrategy, id="cost_based_token_bucket"),
]
"""Custom set of throttle strategy types to test against."""


@pytest.fixture(scope="function", params=STRATEGIES)
def strategy(request) -> ThrottleStrategy[HTTPConnection]:
    return request.param()


@pytest.fixture(scope="function", params=CUSTOM_STRATEGIES)
def custom_strategy(request) -> ThrottleStrategy[HTTPConnection]:
    return request.param()


@pytest.fixture(scope="function")
def utctime() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


FRAMEWORKS = [
    pytest.param(StarletteAdapter, id="starlette"),
    pytest.param(FastAPIAdapter, id="fastapi"),
]
"""Default set of frameworks to test against"""


@pytest.fixture(scope="function", params=FRAMEWORKS)
def web_framework(request) -> ASGIFramework:
    return request.param()
