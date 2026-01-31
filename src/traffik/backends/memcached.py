"""Memcached implementation of a throttle backend using `aiomcache`."""

import asyncio
import functools
import math
import typing
from urllib.parse import urlparse

from aiomcache import Client as MemcachedClient
from aiomcache import ClientException

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, BackendError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    P,
    R,
    T,
    ThrottleErrorHandler,
)
from traffik.utils import fence_token_generator, time


def _on_error_return(
    func: typing.Callable[P, typing.Awaitable[R]],
    return_value: typing.Optional[T] = None,
    hook: typing.Optional[typing.Callable[[Exception], bool]] = None,
) -> typing.Callable[P, typing.Awaitable[typing.Union[R, T, None]]]:
    """
    Decorator to catch `aiomcache.ClientException` and return a specified value.
    Used for handling transient Memcached errors gracefully.

    :param func: Async function to wrap.
    :param return_value: Value to return on exception (default None).
    :param hook: Optional callable to process the exception.
        The hook should return True to suppress re-raising the exception and thus return `return_value`,
        e.g. `hook(exc) -> bool`. Else, the exception is re-raised.
    :param log: Whether to log the exception (default False).
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> typing.Union[R, T, None]:
        try:
            return await func(*args, **kwargs)
        except ClientException as exc:
            if hook and not hook(exc):
                raise
            return return_value

    return wrapper


def _wrap_methods_with_on_error_return(
    obj: typing.Any,
    methods: typing.Iterable[str],
    return_value: typing.Optional[T] = None,
    hook: typing.Optional[typing.Callable[[Exception], bool]] = None,
) -> None:
    """
    Wrap specified methods of the object with `_on_error_return` decorator.

    :param obj: Object whose methods to wrap.
    :param methods: Iterable of method names to wrap.
    :param return_value: Value to return on exception (default None).
    :param hook: Optional callable to process the exception.
    """
    for method_name in methods:
        if hasattr(obj, method_name):
            original_method = getattr(obj, method_name)
            wrapped_method = _on_error_return(
                original_method,
                return_value=return_value,
                hook=hook,
            )
            setattr(obj, method_name, wrapped_method)


class AsyncMemcachedLock:
    """
    Name-based, best-effort, and "instance-reentrant" distributed (un-fair) lock implementation using Memcached's add operation.

    Uses Memcached's atomic `add()` which only succeeds if key doesn't exist.
    This provides a lightweight distributed locking mechanism.

    Suitable for single Memcached instance deployments where low-latency locking is required.
    """

    __slots__ = (
        "_name",
        "_client",
        "_release_timeout",
        "_acquired",
        "_fence_token",
    )

    def __init__(
        self,
        name: str,
        client: MemcachedClient,
        release_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the lock.

        :param name: Unique lock name.
        :param client: `aiomcache.Client` instance.
        :param release_timeout: Lock expiration time in seconds (default 2.0s).
        """
        self._name = name
        self._client = client
        self._release_timeout = math.ceil(release_timeout or 2.0)
        self._acquired = False
        self._fence_token: typing.Optional[str] = None

    def locked(self) -> bool:
        """Return True if this instance currently holds the lock."""
        return self._acquired

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the distributed lock.

        :param blocking: If False, return immediately if locked elsewhere.
        :param blocking_timeout: Max wait time when blocking.
        :return: True if lock acquired, False otherwise.
        """
        if self._acquired:
            # Already acquired by this instance
            return True

        # Memcached has no way to generate fencing tokens natively,
        # we generate our own unique token per acquisition attempt
        # This helps prevent the "stale lock" problem but only
        # per process, not cross-process if clocks are skewed.
        token = str(fence_token_generator.next())
        start = time()
        attempts = 0
        while True:
            # add() is atomic, as only succeeds if key doesn't exist
            # Returns True if added, False if key already exists
            added = await self._client.add(
                self._name.encode(),
                token.encode(),
                exptime=self._release_timeout,
            )
            if added:
                self._acquired = True
                self._fence_token = token
                return True

            # Lock is held by someone else
            if not blocking:
                return False

            # Check timeout
            if blocking_timeout is not None and (time() - start) >= blocking_timeout:
                return False

            # Exponential backoff with jitter
            attempts += 1
            delay = min(0.00001 * attempts, 0.01)
            await asyncio.sleep(delay)

    async def release(self) -> None:
        """Release the lock."""
        if not self._acquired:
            raise RuntimeError(
                f"Cannot release lock '{self._name}'. Lock not owned by this instance"
            )

        try:
            # Ensure we only delete if we own the lock (fence token matches)
            current = await self._client.get(self._name.encode())
            if current and current.decode() == self._fence_token:
                await self._client.delete(self._name.encode())
            else:
                print(f"Warning: Lock '{self._name}' expired or stolen")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Lock might have expired or been released already
            # Log and ignore release errors to avoid deadlocks
            print(f"Warning: Failed to release lock '{self._name}': {str(exc)}")
            pass  # nosec
        finally:
            # Reset state
            self._acquired = False
            self._fence_token = None

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Memcached lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


def _parse_memcached_url(url: str) -> typing.Dict[str, typing.Any]:
    """
    Parse Memcached URL into connection parameters.

    :param url: Memcached URL (e.g. memcached://host:port).
    :return: Dictionary of connection parameters.
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith("memcached"):
        raise ValueError("Invalid Memcached URL scheme")

    host = parsed.hostname or "localhost"
    port = parsed.port or 11211
    return {"host": host, "port": port}


class MemcachedBackend(ThrottleBackend[MemcachedClient, HTTPConnectionT]):
    """
    Memcached-based throttle backend with distributed locking support.

    Uses `aiomcache` for async Memcached operations.

    Note: Memcached has a key size limit of 250 bytes.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        url: typing.Optional[str] = None,
        host: str = "localhost",
        port: int = 11211,
        *,
        pool_size: int = 2,
        pool_minsize: int = 1,
        connection_args: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        namespace: str = ":memcached:",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        track_keys: bool = False,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize Memcached backend.

        :param host: Memcached server host.
        :param port: Memcached server port.
        :param namespace: Key prefix namespace.
        :param pool_size: Connection pool size.
        :param pool_minsize: Minimum pool size.
        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
        :param on_error: Strategy to handle errors during throttling operations.
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters
                and returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.
        :param track_keys: Whether to track all keys in the namespace for clearing.
            Since Memcached doesn't support key listing like Redis, this enables
            a best-effort tracking mechanism using a special tracking key to store
            all keys set by this backend. This allows the `clear()` method to function.
            Note: This adds overhead to cache operations and is not 100% reliable.
            Only use if you absolutely need the `clear()` functionality.
            `clear()` will be no-op if this is False.
        :param kwargs: Additional keyword arguments.
        """
        if url and (host != "localhost" or port != 11211):
            raise ValueError("Specify either 'url' or 'host'/'port', not both.")

        if url is not None:
            params = _parse_memcached_url(url)
            host = params["host"]
            port = params["port"]

        self.host = host
        self.port = port
        self.pool_size = pool_size
        self.pool_minsize = pool_minsize
        self.connection_args = connection_args
        self.track_keys = track_keys
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
            on_error=on_error,
            **kwargs,
        )

    def _get_tracking_key(self) -> str:
        """Get the key used to track all keys in this namespace."""
        return f"{self.namespace}:__tracked_keys__"

    async def _track_key(self, key: str) -> None:
        """Best-effort add key to tracking set."""
        if self.connection is None:
            return

        if "||" in key:
            print(
                f"Warning: Key '{key}' contains '||' character which is used as separator in tracking."
                " Ensure keys do not contain this sequence."
            )

        tracking_key = self._get_tracking_key()
        try:
            tracked = await self.connection.get(tracking_key.encode())
            if tracked is None:
                await self.connection.set(
                    tracking_key.encode(),
                    key.encode(),
                    exptime=0,
                )
            else:
                keys_set = set(tracked.decode().split("||"))
                if key not in keys_set:
                    keys_set.add(key)
                    new_tracked = "||".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
        except Exception as exc:
            print(f"Warning: Failed to track key '{key}': {exc}")

    async def _untrack_key(self, key: str) -> None:
        """Best-effort remove key from tracking set."""
        if self.connection is None:
            return

        tracking_key = self._get_tracking_key()
        try:
            tracked = await self.connection.get(tracking_key.encode())
            if tracked is None:
                return

            keys_set = set(tracked.decode().split("||"))
            if key in keys_set:
                keys_set.remove(key)
                if keys_set:
                    new_tracked = "||".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
                else:
                    await self.connection.delete(tracking_key.encode())
        except Exception as exc:
            print(f"Warning: Failed to untrack key '{key}': {exc}")

    async def initialize(self) -> None:
        """Initialize the Memcached connection."""
        if self.connection is None:
            self.connection = MemcachedClient(
                self.host,
                self.port,
                pool_size=self.pool_size,
                pool_minsize=self.pool_minsize,
                conn_args=self.connection_args,
            )
            # If key doesn't exist, `incr` & `decr` raises a specific
            # `ClientException`. We catch that and return None instead
            _wrap_methods_with_on_error_return(
                self.connection,
                methods=["incr", "decr"],
                hook=lambda exc: b"NOT_FOUND" in str(exc).encode(),
                return_value=None,
            )

    async def ready(self) -> bool:
        if self.connection is None:
            return False

        try:
            await self.connection.version()
            return True
        except ClientException:
            return False

    async def get_lock(self, name: str) -> AsyncMemcachedLock:
        """
        Get a distributed lock for the given name.

        :param name: Lock name.
        :return: AsyncMemcachedLock instance.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )
        return AsyncMemcachedLock(name, client=self.connection, release_timeout=2.0)

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """
        Get value by key.

        :param key: Key to retrieve.
        :return: Value as string, or None if not found.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        value = await self.connection.get(key.encode())
        if value is None:
            return None
        return value.decode()

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """
        Set value by key.

        :param key: Key to set.
        :param value: Value to store.
        :param expire: Optional TTL in seconds.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        exptime = int(expire) if expire is not None else 0
        await self.connection.set(
            key.encode(),
            str(value).encode(),
            exptime=exptime,
        )
        if self.track_keys:
            await self._track_key(key)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """
        Delete key if exists.

        :param key: Key to delete.
        :return: True if deleted, False if not found.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        deleted = await self.connection.delete(key.encode())
        if deleted and self.track_keys:
            await self._untrack_key(key)
        return deleted

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        Memcached's `incr` is atomic and thread-safe across all clients.

        :param key: Counter key.
        :param amount: Amount to increment by.
        :return: New value after increment.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        # Try to increment existing counter
        new_value = await self.connection.incr(key.encode(), amount)
        if new_value is not None:
            return new_value

        # Key doesn't exist, initialize it
        # Use add() to atomically create if not exists
        added = await self.connection.add(
            key.encode(),
            str(amount).encode(),
            exptime=0,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return amount

        # Someone else created it, try increment again
        new_value = await self.connection.incr(key.encode(), amount)
        return new_value  # type: ignore[return-value]

    async def decrement(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement counter.

        Memcached's `decr` is atomic and thread-safe across all clients.
        Note: Memcached counters cannot go below 0.

        :param key: Counter key.
        :param amount: Amount to decrement by.
        :return: New value after decrement.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        # Try to decrement existing counter
        new_value = await self.connection.decr(key.encode(), amount)
        if new_value is not None:
            return new_value

        # Key doesn't exist, initialize it to 0 - amount
        # Use add() to atomically create if not exists
        added = await self.connection.add(
            key.encode(),
            str(-amount).encode(),
            exptime=0,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return -amount

        # Someone else created it, try decrement again
        new_value = await self.connection.decr(key.encode(), amount)
        return new_value  # type: ignore[return-value]

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        Note: Memcached doesn't have a native "expire" command.
        We need to get the value and set it again with new TTL.

        :param key: Key to set expiration on.
        :param seconds: TTL in seconds.
        :return: True if expiration was set, False if key doesn't exist.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        # Get current value
        value = await self.connection.get(key.encode())
        if value is None:
            return False

        # Set with new expiration
        is_set = await self.connection.set(
            key.encode(),
            value,
            exptime=seconds,
        )
        return is_set

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomic increment with TTL.

        For Memcached, we need to handle this specially since `incr` doesn't
        update expiration time.

        :param key: Counter key.
        :param amount: Amount to increment.
        :param ttl: TTL in seconds (only applied on first set).
        :return: New value after increment.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        # Try to increment existing counter
        new_value = await self.connection.incr(key.encode(), amount)
        if new_value is not None:
            return new_value

        # Key doesn't exist, create with TTL
        # Atomically create with TTL
        added = await self.connection.add(
            key.encode(),
            str(amount).encode(),
            exptime=ttl,
        )
        if added:
            if self.track_keys:
                await self._track_key(key)
            return amount

        # Someone else created it, increment
        new_value = await self.connection.incr(key.encode(), amount)
        return new_value  # type: ignore[return-value]

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get multiple keys.

        :param keys: Keys to retrieve.
        :return: List of values (None for missing keys), same order as keys.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not keys:
            return []

        encoded_keys = [k.encode() for k in keys]
        values = await self.connection.multi_get(*encoded_keys)
        results: typing.List[typing.Optional[str]] = []
        for value in values:
            if value is not None:
                results.append(value.decode())
            else:
                results.append(None)
        return results

    async def clear(self) -> None:
        """
        Clear all tracked keys in the namespace.

        Note: This only works if `track_keys` was enabled.
        If not enabled, this is a no-op. If the Memcached server
        is only used for this backend, consider flushing the entire cache instead.
        Override this method as so.

        ```python
        async def clear(self) -> None:
            # Flush entire Memcached cache, if not tracking keys
            if self.connection is not None and not self.track_keys:
                await self.connection.flush_all()
                return
            await super().clear()
        ```
        """
        if not self.track_keys:
            # No-op if not tracking keys
            return

        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        tracking_key = self._get_tracking_key()
        tracked = await self.connection.get(tracking_key.encode())
        if tracked is None:
            return

        keys = tracked.decode().split("||")
        delete_tasks = []
        for key in keys:
            delete_tasks.append(self.connection.delete(key.encode()))

        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                raise BackendError(
                    f"Failed to clear key '{key}': {str(result)}"
                ) from result

        # Delete the tracking key itself finally
        await self.connection.delete(tracking_key.encode())

    async def reset(self) -> None:
        """Reset the backend."""
        await self.clear()

    async def close(self) -> None:
        """Close the Memcached connection."""
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
