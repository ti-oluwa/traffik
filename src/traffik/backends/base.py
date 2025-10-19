"""Base classes and utilities for throttle backends."""

import asyncio
import functools
import hashlib
import inspect
import math
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp
from typing_extensions import Self

from traffik.exceptions import AnonymousConnection, BackendError, ConnectionThrottled
from traffik.types import (
    AsyncLock,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    P,
    R,
    T,
    WaitPeriod,
)
from traffik.utils import AsyncLockContext, get_ip_address


async def connection_identifier(connection: HTTPConnection) -> str:
    client_ip = get_ip_address(connection)
    if not client_ip:
        raise AnonymousConnection("Unable to determine client IP from connection")
    return f"{client_ip.exploded}:{connection.scope['path']}"


async def connection_throttled(
    connection: HTTPConnection, wait_ms: WaitPeriod, *args, **kwargs
) -> typing.NoReturn:
    """
    Handler for throttled HTTP connections

    :param connection: The HTTP connection
    :param wait_ms: The wait period in milliseconds before the next connection can be made
    :return: None
    """
    wait_seconds = math.ceil(wait_ms / 1000)
    raise ConnectionThrottled(
        wait_period=wait_seconds,
        detail=kwargs.get(
            "detail", f"Too many requests. Retry in {wait_seconds} seconds."
        ),
        status_code=kwargs.get("status_code", 429),
        headers=kwargs.get("headers", None),
    )


throttle_backend_ctx: ContextVar[typing.Optional["ThrottleBackend"]] = ContextVar(
    "throttle_backend_ctx", default=None
)

BACKEND_STATE_KEY = "__traffik_throttle_backend"


def build_key(*args: typing.Any, **kwargs: typing.Any) -> str:
    """Builds a key using the provided parameters."""
    key_parts = [str(arg) for arg in args]
    key_parts.extend(f"{k}={v}" for k, v in kwargs.items())
    if not key_parts:
        return "*"
    key_parts.sort()  # Sort to ensure consistent ordering
    return hashlib.md5(":".join(key_parts).encode()).hexdigest()  # nosec


def _raises_error(
    func: typing.Callable[P, R],
    target_exc_type: typing.Type[BaseException] = BaseException,
) -> typing.Callable[P, R]:
    """Decorator"""
    if getattr(func, "_error_wrapped_", None):
        # Already wrapped
        return func

    wrapper: typing.Union[
        typing.Callable[P, R], typing.Callable[P, typing.Awaitable[R]]
    ]
    if inspect.iscoroutinefunction(func):

        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore[no-redefined]
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except target_exc_type as exc:
                if isinstance(exc, BackendError):
                    raise
                raise BackendError(
                    f"Error occurred in backend operation. {exc}"
                ) from exc

        wrapper = async_wrapper
    else:

        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except target_exc_type as exc:
                if isinstance(exc, BackendError):
                    raise
                raise BackendError(
                    f"Error occurred in backend operation. {exc}"
                ) from exc

        wrapper = sync_wrapper

    wrapper._error_wrapped_ = True  # type: ignore
    return functools.update_wrapper(wrapper, func)  # type: ignore


class ThrottleBackend(typing.Generic[T, HTTPConnectionT]):
    """
    Base class for throttle backends.

    Subclasses must implement the following methods:

    - initialize(): Setup backend connection/resources
    - get(key): Get value for key (Must not implement implicit locking).
    - set(key, value, expire): Set value for key with optional expiration (Must not implement implicit locking).
    - delete(key): Remove key (Must not implement implicit locking).
    - get_lock(key, timeout): Acquire a distributed lock for key
    - increment(key, amount): Atomically increment counter
    - decrement(key, amount): Atomically decrement counter
    - expire(key, seconds): Set expiration on existing key
    - reset(): Clear all throttling data

    The `get()`, `set()`, and `delete()` methods need explicit locking when utilized in racy conditions.
    Hence, locks should not be implemented implicitly in these methods.
    """

    base_exception_type: typing.ClassVar[typing.Type[BaseException]] = BaseException
    """
    The base exception type that backend operations may raise.

    This is used to wrap backend methods to re-raise exceptions as `traffik.exceptions.BackendError`
    """
    _default_wrap_methods: typing.ClassVar[typing.Tuple[str, ...]] = (
        "initialize",
        "get",
        "set",
        "delete",
        "get_lock",
        "increment",
        "decrement",
        "expire",
        "reset",
        "multi_get",
        "increment_with_ttl",
        "close",
    )
    """Default methods to wrap for error handling."""
    wrap_methods: typing.Tuple[str, ...] = ()
    """Additional methods to wrap for error handling. Meant to be overridden/defined by subclasses."""

    def __init_subclass__(cls) -> None:
        method_names = set(cls._default_wrap_methods).union(cls.wrap_methods)
        for method_name in method_names:
            method = getattr(cls, method_name, None)
            if method is not None and callable(method):
                setattr(
                    cls,
                    method_name,
                    _raises_error(
                        func=method,
                        target_exc_type=cls.base_exception_type,
                    ),
                )
            else:
                raise RuntimeError(
                    f"Cannot wrap method {method_name!r}. Ensure {method_name!r} is a defined method on {cls.__name__!r}."
                )

    def __init__(
        self,
        connection: typing.Optional[T],
        *,
        namespace: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        """
        Initialize the throttle backend with a prefix.

        :param connection: The connection to the backend (e.g., Redis).
        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
        """
        self.connection = connection
        self.namespace = namespace
        self.identifier = identifier or connection_identifier
        self.handle_throttled = handle_throttled or connection_throttled
        self.persistent = persistent

    async def get_key(self, key: str, *args, **kwargs) -> str:
        """
        Return a namespaced key.

        :param key: The throttling key.
        :return: The full key with namespace.
        """
        if args or kwargs:
            base_key = build_key(key, *args, **kwargs)
            return f"{self.namespace}:{base_key}"
        return f"{self.namespace}:{key}"

    async def initialize(self) -> None:
        """
        Initialize the throttle backend ensuring it is ready for use.

        Subclasses must implement this method to set up connections,
        create tables/collections, or perform any necessary setup.
        """
        raise NotImplementedError("`initialize()` must be implemented by the backend.")

    async def get(self, key: str, *args, **kwargs) -> typing.Optional[str]:
        """
        Get the value for the given key.

        :param key: The throttling key to retrieve.
        :return: The value associated with the key, or None if key doesn't exist.
        """
        raise NotImplementedError("`get()` must be implemented by the backend.")

    async def set(
        self, key: str, value: str, expire: typing.Optional[int] = None
    ) -> None:
        """
        Set the value for the given key with optional expiration.

        :param key: The throttling key to set.
        :param value: The value to set.
        :param expire: Optional expiration time in seconds.
        """
        raise NotImplementedError("`set()` must be implemented by the backend.")

    async def delete(self, key: str, *args, **kwargs) -> bool:
        """
        Delete the given key.

        :param key: The throttling key to delete.
        :return: True if key was deleted, False if key didn't exist.
        """
        raise NotImplementedError("`delete()` must be implemented by the backend.")

    async def get_lock(self, name: str) -> AsyncLock:
        """
        Acquire a distributed lock with the given name.

        :param name: The name of the lock.
        :return: An asynchronous lock object that implements the `traffik.types.AsyncLock` protocol.
        """
        raise NotImplementedError("`lock()` must be implemented by the backend.")

    async def lock(
        self,
        name: str,
        release_timeout: typing.Optional[float] = None,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> AsyncLockContext[AsyncLock]:
        """
        Context manager to acquire a distributed lock for the given key.

        :param name: The name of the lock.
        :param release_timeout: Lock timeout in seconds. If the lock is not released within this time, it will be automatically released.
        :param blocking: If False, do not wait for the lock if it's already held, raise a timeout error instead.
        :param blocking_timeout: Maximum time in seconds to wait for the lock if blocking is True. None means wait indefinitely.
        :return: An asynchronous context manager that acquires/releases the lock.
        """
        lock_name = await self.get_key(name)
        lock = await self.get_lock(lock_name)
        return AsyncLockContext(
            lock,
            release_timeout=release_timeout,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )

    async def reset(self) -> None:
        """
        Atomic reset of all throttling data for this backend.
        """
        raise NotImplementedError("`reset()` must be implemented by the backend.")

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment a counter and return the NEW value.

        This operation MUST be atomic (thread-safe across processes).
        If key doesn't exist, initialize to 0 then increment.

        :param key: Counter key
        :param amount: Amount to increment by (default 1)
        :return: New value after increment

        Example:
        ```python
        counter = await backend.increment("user:123:counter")
        # If counter was 5, returns 6
        # If counter didn't exist, returns 1
        ```
        """
        raise NotImplementedError(
            "`increment()` must be implemented by the backend for atomic operations."
        )

    async def decrement(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement a counter and return the NEW value.

        This operation MUST be atomic (thread-safe across processes).
        If key doesn't exist, initialize to 0 then decrement.

        :param key: Counter key
        :param amount: Amount to decrement by (default 1)
        :return: New value after decrement

        Example:
        ```python
        counter = await backend.decrement("user:123:counter")
        # If counter was 5, returns 4
        # If counter didn't exist, returns -1
        ```
        """
        return await self.increment(key, -amount)

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration time on an existing key.

        :param key: Key to set expiration on
        :param seconds: TTL in seconds
        :return: True if expiration was set, False if key doesn't exist
        """
        raise NotImplementedError(
            "`expire()` must be implemented by the backend for atomic operations."
        )

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomically increment and set TTL if key doesn't exist.

        Default implementation uses increment() + expire(), but backends
        can override for better performance (e.g., Redis pipeline).

        :param key: Counter key
        :param amount: Amount to increment
        :param ttl: TTL to set if key is new (seconds)
        :return: New value after increment
        """
        value = await self.increment(key, amount)
        if value == amount:
            # This was the first increment (new key)
            await self.expire(key, ttl)
        return value

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Atomically get multiple keys in one operation.

        This should be a snapshot of all keys at a single point in time.
        but backends can override for better performance.

        :param keys: Sequence of keys to retrieve
        :return: List of values (None for missing keys), same order as keys

        Note: This is different from non-atomic batch get - values should be
        consistent snapshot, not interleaved with writes.
        """
        results = []
        for key in keys:
            value = await self.get(key)
            results.append(value)
        return results

    async def close(self) -> None:
        """
        Close the backend connection and perform cleanup.

        This should always set `connection` to None.

        Override this to close connections, flush buffers, etc.
        """
        pass

    @asynccontextmanager
    async def lifespan(self, app: ASGIApp) -> typing.AsyncIterator[None]:
        """
        ASGI lifespan context manager for the throttle backend.
        """
        async with self(app):
            yield

    def __call__(
        self,
        app: typing.Optional[ASGIApp] = None,
        persistent: typing.Optional[bool] = None,
        close_on_exit: typing.Optional[bool] = None,
    ) -> "ThrottleContext[Self]":
        """
        Create a throttle context for the backend.

        **Warning!!!**: Avoid nesting a non-persistent context inside a persistent context from the
        same backend. This could lead to unexpected behaviour and data losss due to nested non-persistence.

        :param app: The ASGI application to assign the backend to.
        :param persistent: Whether to keep the backend state across application restarts.
            This overrides the backend's persistent setting if set.
            If None, for non-nested contexts, the backend's persistence settings is used.
            For nested contexts, context is persistent if outer context's backend
            is the same as this context's backend.
            This is so the inner context does not clear the outer context's data unintentionally.

        :param close_on_exit: Whether to close the backend when exiting the context.
            If None, context will auto-close on exit, except if nested within another context.
        :return: A context manager for the throttle backend.
        """
        if app is not None:
            # Ensure app.state exists
            app.state = getattr(app, "state", {})  # type: ignore
            setattr(app.state, BACKEND_STATE_KEY, self)  # type: ignore

        parent_backend = get_throttle_backend(app)
        is_inner_context = parent_backend is not None

        if persistent is not None:
            context_persistence = persistent

        # For non-nested contexts, use the backend's persistence settings.
        # For nested contexts, context is persistent if outer context's backend
        # is the same as this context's backend.
        # This is so the inner context does not clear the outer context's data unintentionally.
        elif is_inner_context is False or parent_backend is not self:
            context_persistence = self.persistent
        else:
            context_persistence = True

        if close_on_exit is not None:
            context_close_on_exit = close_on_exit
        else:
            # Context should not close on exit if it is nested inside another context
            context_close_on_exit = is_inner_context is False
        return ThrottleContext(
            backend=self,
            persistent=context_persistence,
            close_on_exit=context_close_on_exit,
        )


ThrottleBackendTco = typing.TypeVar(
    "ThrottleBackendTco", bound=ThrottleBackend, covariant=True
)


class ThrottleContext(typing.Generic[ThrottleBackendTco]):
    """
    Context manager for throttle backends.
    """

    def __init__(
        self,
        backend: ThrottleBackendTco,
        persistent: bool = False,
        close_on_exit: bool = True,
    ) -> None:
        self.backend = backend
        self.persistent = persistent
        self.close_on_exit = close_on_exit
        self._context_token: typing.Optional[
            Token[typing.Optional[ThrottleBackend]]
        ] = None

    async def __aenter__(self) -> ThrottleBackendTco:
        backend = self.backend
        await backend.initialize()
        # Set the throttle backend in the context variable
        self._context_token = throttle_backend_ctx.set(backend)
        return backend

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self._context_token is not None:
            throttle_backend_ctx.reset(self._context_token)
            self._context_token = None

        backend = self.backend
        if not self.persistent:
            await backend.reset()

        if self.close_on_exit:
            await backend.close()


def get_throttle_backend(
    app: typing.Optional[ASGIApp] = None,
) -> typing.Optional[ThrottleBackend[typing.Any, HTTPConnection]]:
    """
    Get the current context's throttle backend or provided app's throttle backend.

    :param app: The ASGI application to check for a throttle backend.
    :return: The current throttle backend or None if not set.
    """
    # Try to get from contextvar, then check `app.state`
    backend = throttle_backend_ctx.get(None)
    if (
        backend is None
        and app is not None
        and (app_state := getattr(app, "state", None)) is not None
    ):
        backend = getattr(app_state, BACKEND_STATE_KEY, None)
    return typing.cast(
        typing.Optional[ThrottleBackend[typing.Any, HTTPConnection]], backend
    )


__all__ = [
    "ThrottleBackend",
    "connection_identifier",
    "connection_throttled",
    "get_throttle_backend",
]
