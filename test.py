# traffik/backends/base.py

from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
import hashlib
import math
import typing

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp
from typing_extensions import Self

from traffik._utils import get_ip_address
from traffik.exceptions import AnonymousConnection, ConnectionThrottled
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    T,
    WaitPeriod,
)


async def connection_identifier(connection: HTTPConnection) -> str:
    client_ip = get_ip_address(connection)
    if not client_ip:
        raise AnonymousConnection("Unable to determine client IP from connection")
    return f"{client_ip.exploded}:{connection.scope['path']}"


async def connection_throttled(
    connection: HTTPConnection, wait_period: WaitPeriod, *args, **kwargs
) -> typing.NoReturn:
    """
    Handler for throttled HTTP connections

    :param connection: The HTTP connection
    :param wait_period: The wait period in milliseconds before the next connection can be made
    :return: None
    """
    wait_seconds = math.ceil(wait_period / 1000)
    raise ConnectionThrottled(wait_period=wait_seconds)


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
    return hashlib.md5(":".join(key_parts).encode()).hexdigest()


class ThrottleBackend(typing.Generic[T, HTTPConnectionT]):
    """
    Base class for throttle backends.
    
    Subclasses must implement atomic operations only:
    - initialize(): Setup backend connection/resources
    - increment(key, amount): Atomically increment counter
    - decrement(key, amount): Atomically decrement counter
    - set_if_not_exists(key, value, expire): Atomically set key only if it doesn't exist
    - get_and_set(key, value, expire): Atomically get old value and set new value
    - compare_and_set(key, expected, new, expire): Atomically compare and swap
    - expire(key, seconds): Set expiration on existing key
    - delete(key): Remove key
    - clear(): Remove all keys
    
    Non-atomic operations (get/set) are intentionally excluded to prevent race conditions.
    """

    def __init__(
        self,
        connection: T,
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
        Get the full key for the given throttling key.

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

    async def delete(self, key: str, *args, **kwargs) -> bool:
        """
        Delete the given key.

        :param key: The throttling key to delete.
        :return: True if key was deleted, False if key didn't exist.
        """
        raise NotImplementedError("`delete()` must be implemented by the backend.")

    async def clear(self) -> None:
        """
        Clear all keys in the backend namespace.

        This function clears all keys that match the backend's namespace.
        Useful for testing or resetting rate limits.
        """
        raise NotImplementedError("`clear()` must be implemented by the backend.")

    # ===== ATOMIC OPERATIONS (Required for thread-safe rate limiting) =====

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment a counter and return the NEW value.
        
        This operation MUST be atomic (thread-safe across processes).
        If key doesn't exist, initialize to 0 then increment.
        
        :param key: Counter key
        :param amount: Amount to increment by (default 1)
        :return: New value after increment
        
        Example:
            counter = await backend.increment("user:123:counter")
            # If counter was 5, returns 6
            # If counter didn't exist, returns 1
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
            counter = await backend.decrement("user:123:counter")
            # If counter was 5, returns 4
            # If counter didn't exist, returns -1
        """
        new_value = await self.increment(key, -amount)
        return new_value

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

    async def set_if_not_exists(
        self, 
        key: str, 
        value: str, 
        expire: typing.Optional[int] = None
    ) -> bool:
        """
        Set key only if it doesn't exist (atomic operation).
        
        This operation MUST be atomic.
        
        :param key: Key to set
        :param value: Value to set
        :param expire: Optional TTL in seconds
        :return: True if key was set, False if key already existed
        
        Example:
            # Try to claim a window
            success = await backend.set_if_not_exists("window:user123", "12345", 60)
            if success:
                # We claimed the window
            else:
                # Someone else has the window
        """
        raise NotImplementedError(
            "`set_if_not_exists()` must be implemented by the backend for atomic operations."
        )

    async def get_and_set(
        self,
        key: str,
        value: str,
        expire: typing.Optional[int] = None
    ) -> typing.Optional[str]:
        """
        Atomically get the old value and set new value.
        
        This operation MUST be atomic (read-modify-write in one operation).
        
        :param key: Key to get and set
        :param value: New value to set
        :param expire: Optional TTL in seconds
        :return: Old value if key existed, None if key didn't exist
        
        Example:
            # Atomically swap token bucket state
            old_state = await backend.get_and_set("bucket:user123", new_state_json, 60)
            if old_state:
                # Parse old state and calculate tokens
            else:
                # First request, bucket is full
        """
        raise NotImplementedError(
            "`get_and_set()` must be implemented by the backend for atomic operations."
        )

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        new_value: str,
        expire: typing.Optional[int] = None
    ) -> bool:
        """
        Atomically compare and set (CAS operation).
        
        Only sets new_value if current value equals expected.
        This operation MUST be atomic.
        
        :param key: Key to compare and set
        :param expected: Expected current value
        :param new_value: New value to set if current matches expected
        :param expire: Optional TTL in seconds
        :return: True if value was set (current == expected), False otherwise
        
        Example:
            # Optimistic locking for token bucket
            success = await backend.compare_and_set(
                "bucket:user123",
                old_state_json,
                new_state_json,
                60
            )
            if not success:
                # Value changed, retry
        """
        raise NotImplementedError(
            "`compare_and_set()` must be implemented by the backend for atomic operations."
        )

    # ===== OPTIONAL OPTIMIZATIONS =====

    async def increment_with_ttl(
        self, 
        key: str, 
        amount: int = 1, 
        ttl: int = 60
    ) -> int:
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

    async def multi_get_atomic(
        self, 
        keys: typing.Sequence[str]
    ) -> typing.List[typing.Optional[str]]:
        """
        Atomically get multiple keys in one operation.
        
        This should be a snapshot of all keys at a single point in time.
        Default implementation calls get_and_set with same value (no-op swap),
        but backends can override for better performance.
        
        :param keys: List of keys to get
        :return: List of values (None for missing keys), same order as keys
        
        Note: This is different from non-atomic batch get - values should be
        consistent snapshot, not interleaved with writes.
        """
        results = []
        for key in keys:
            # Get by swapping with same value (no-op but atomic)
            value = await self.get_and_set(key, "", None)
            if value == "":
                # Key didn't exist
                results.append(None)
            elif value is not None:
                # Restore original value
                await self.get_and_set(key, value, None)
                results.append(value)
            else:
                results.append(None)
        return results

    # ===== LIFECYCLE METHODS =====

    async def reset(self) -> None:
        """
        Reset the throttle backend by clearing all keys.
        
        Calls clear() by default. Override if additional cleanup is needed.
        """
        await self.clear()

    async def close(self) -> None:
        """
        Close the backend connection and perform cleanup.
        
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

        :param app: The ASGI application to assign the backend to.
        :param persistent: Whether to keep the backend state across application restarts.
            This overrides the backend's persistent setting if set.
        :param close_on_exit: Whether to close the backend when exiting the context.
        :return: A context manager for the throttle backend.
        """
        if app is not None:
            # Ensure app.state exists
            app.state = getattr(app, "state", {})  # type: ignore
            setattr(app.state, BACKEND_STATE_KEY, self)  # type: ignore

        return ThrottleContext(
            backend=self,
            persistent=persistent if persistent is not None else self.persistent,
            close_on_exit=close_on_exit
            if close_on_exit is not None
            else (get_throttle_backend() is self if app is None else get_throttle_backend(app) is self),
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
            # Reset the backend if not persistent
            await backend.reset()

        if self.close_on_exit:
            await backend.close()


def get_throttle_backend(
    connection: typing.Optional[HTTPConnectionT] = None,
) -> typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]]:
    """
    Get the current context's throttle backend or provided connection's throttle backend.

    :param connection: The HTTP connection to check for a throttle backend.
    :return: The current throttle backend or None if not set.
    """
    # Try to get from contextvar, then check `connection.app.state`
    backend = throttle_backend_ctx.get(None)
    if backend is None and connection is not None:
        backend = getattr(connection.app.state, BACKEND_STATE_KEY, None)
    return typing.cast(
        typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]], backend
    )


__all__ = [
    "ThrottleBackend",
    "connection_identifier",
    "connection_throttled",
    "get_throttle_backend",
]
