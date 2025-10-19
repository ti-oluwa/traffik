"""Global test configuration and fixtures."""

import logging
import os
import typing

import pytest

from traffik.backends.base import ThrottleBackend
from traffik.backends.inmemory import InMemoryBackend


REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
MEMCACHED_HOST = os.getenv("MEMCACHED_HOST", "localhost")
MEMCACHED_PORT = int(os.getenv("MEMCACHED_PORT", "11211"))
MEMCACHED_POOL_SIZE = int(os.getenv("MEMCACHED_POOL_SIZE", "4"))


class SkipBackend(Exception):
    """Raised to skip a backend test if the backend is not available."""

    pass


def get_inmemory_backend(namespace: str, persistent: bool) -> InMemoryBackend:
    return InMemoryBackend(persistent=persistent, namespace=namespace)


def get_redis_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_REDIS_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend("Skipping Redis backend tests as per environment setting.")

    from traffik.backends.redis import RedisBackend

    return RedisBackend(
        connection=REDIS_URL, namespace=namespace, persistent=persistent
    )


def get_memcached_backend(namespace: str, persistent: bool) -> ThrottleBackend:
    if os.getenv("SKIP_MEMCACHED_TESTS", "false").lower() in ("1", "true", "yes", "t"):
        raise SkipBackend(
            "Skipping Memcached backend tests as per environment setting."
        )

    from traffik.backends.memcached import MemcachedBackend

    return MemcachedBackend(
        host=MEMCACHED_HOST,
        port=MEMCACHED_PORT,
        namespace=namespace,
        pool_size=MEMCACHED_POOL_SIZE,
        persistent=persistent,
    )


BACKEND_FACTORIES: typing.List[typing.Callable[[str, bool], ThrottleBackend]] = [
    get_memcached_backend,
    get_redis_backend,
    get_inmemory_backend,
]


class BackendGen:
    """Wraps a backend generator function to provide both iteration and call capabilities."""

    def __init__(
        self, func: typing.Callable[[str, bool], typing.Iterator[ThrottleBackend]]
    ) -> None:
        self._func = func

    def __iter__(self) -> typing.Iterator[ThrottleBackend]:
        return iter(self())

    def __call__(
        self,
        *,
        namespace: str = "test",
        persistent: bool = False,
        exclude: typing.Optional[
            typing.Union[
                typing.Type[ThrottleBackend],
                typing.Tuple[typing.Type[ThrottleBackend], ...],
            ]
        ] = None,
    ) -> typing.Generator[ThrottleBackend, None, None]:
        for backend in self._func(namespace, persistent):
            if exclude is not None and isinstance(backend, exclude):
                continue
            yield backend


@pytest.fixture(scope="function")
def backends() -> BackendGen:
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

    return BackendGen(gen_func)


@pytest.fixture(scope="function")
def inmemory_backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture(scope="function")
async def backend() -> InMemoryBackend:
    backend = InMemoryBackend(persistent=False)
    return backend
