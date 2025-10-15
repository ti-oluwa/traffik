import logging
import os
import typing

import pytest

from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"


class SkipBackend(Exception):
    """Raised to skip a backend test if the backend is not available."""

    pass


def get_inmemory_backend(namespace: str, persistent: bool) -> InMemoryBackend:
    return InMemoryBackend(persistent=persistent, namespace=namespace)


def get_redis_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if bool(os.getenv("SKIP_REDIS_TESTS")):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")

    from redis.asyncio import Redis

    from traffik.backends.redis import RedisBackend

    redis = Redis.from_url(REDIS_URL, decode_responses=False)
    return RedisBackend(connection=redis, namespace=namespace, persistent=persistent)


def get_memcached_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if bool(os.getenv("SKIP_MEMCACHED_TESTS")):
        raise SkipBackend(
            "Skipping Memcached backend tests as per environment setting."
        )

    from traffik.backends.memcached import MemcachedBackend

    return MemcachedBackend(namespace=namespace, persistent=persistent)


BACKEND_FACTORIES: typing.List[typing.Callable[[str, bool], ThrottleBackend]] = [
    get_memcached_backend,
    get_redis_backend,
    get_inmemory_backend,
]


class BackendsGen:
    """Wraps a backend generator function to provide both iteration and call capabilities."""

    def __init__(
        self, func: typing.Callable[[str, bool], typing.Iterator[ThrottleBackend]]
    ) -> None:
        self._func = func

    def __iter__(self) -> typing.Iterator[ThrottleBackend]:
        return iter(self())

    def __call__(
        self, namespace: str = "test", persistent: bool = False
    ) -> typing.Iterator[ThrottleBackend]:
        return self._func(namespace, persistent)


@pytest.fixture(scope="function")
def backends() -> BackendsGen:
    def gen_func(
        namespace: str = "test", persistent: bool = False
    ) -> typing.Generator[ThrottleBackend, None, None]:
        for backend_factory in BACKEND_FACTORIES:
            try:
                backend = backend_factory(namespace, persistent)
            except SkipBackend as exc:
                logging.debug(f"Skipping backend: {exc}")
                continue
            yield backend

    return BackendsGen(gen_func)


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()
