"""Global test configuration and fixtures."""

import logging
import os
import typing

import pytest

from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
MEMCACHED_HOST = os.getenv("MEMCACHED_HOST", "localhost")
MEMCACHED_PORT = int(os.getenv("MEMCACHED_PORT", "11211"))
MEMCACHED_POOL_SIZE = int(os.getenv("MEMCACHED_POOL_SIZE", "2"))


class SkipBackend(Exception):
    """Raised to skip a backend test if the backend is not available."""

    pass


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
    )


def get_coredis_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_REDIS_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")

    from traffik.backends.redis.coredis import RedisBackend

    return RedisBackend(
        connection=REDIS_URL,
        namespace=namespace,
        persistent=persistent,
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
    )


def get_multiprocess_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    from traffik.backends.multiprocess import MultiProcessInMemoryBackend

    return MultiProcessInMemoryBackend(
        namespace=namespace,
        number_of_shards=8,
        max_keys=1024,
        cleanup_frequency=2.0,
    )


BACKEND_FACTORIES: typing.List[
    typing.Callable[[str, bool], ThrottleBackend[typing.Any, typing.Any]]
] = [
    get_inmemory_backend,
    get_multiprocess_backend,
    get_emcache_backend,
    get_aiomcache_backend,
    get_aioredis_backend,
    get_coredis_backend,
]


class BackendGen:
    """Wraps a backend generator function to provide both iteration and call capabilities."""

    def __init__(
        self,
        func: typing.Callable[
            [str, bool], typing.Iterator[ThrottleBackend[typing.Any, typing.Any]]
        ],
    ) -> None:
        self._func = func

    def __iter__(self) -> typing.Iterator[ThrottleBackend[typing.Any, typing.Any]]:
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
    ) -> typing.Generator[ThrottleBackend[typing.Any, typing.Any], None, None]:
        for backend in self._func(namespace, persistent):
            if exclude is not None and isinstance(backend, exclude):
                continue
            yield backend


def _backend_gen(
    namespace: str = "test", persistent: bool = False
) -> typing.Generator[ThrottleBackend[typing.Any, typing.Any], None, None]:
    for backend_factory in BACKEND_FACTORIES:
        try:
            backend = backend_factory(namespace, persistent)
        except SkipBackend as exc:
            logger.debug(f"Skipping backend: {exc}")
            continue
        yield backend


@pytest.fixture(scope="function")
def backends() -> BackendGen:
    return BackendGen(_backend_gen)


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def backend() -> typing.AsyncGenerator[InMemoryBackend, None]:
    backend = InMemoryBackend()
    async with backend(close_on_exit=True):
        yield backend
