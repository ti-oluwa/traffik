"""Memcached implementation of a throttle backend using `aiomcache`."""

import asyncio
import functools
import logging
import typing

from aiomcache import Client as MemcachedClient, ClientException

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    P,
    R,
    T,
)
from traffik.utils import time

logger = logging.getLogger(__name__)


def _on_error_return(
    func: typing.Callable[P, typing.Awaitable[R]],
    return_value: T = None,
    hook: typing.Optional[typing.Callable[[Exception], bool]] = None,
    log: bool = False,
) -> typing.Callable[P, typing.Awaitable[typing.Union[R, T]]]:
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
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> typing.Union[R, T]:
        try:
            return await func(*args, **kwargs)
        except ClientException as exc:
            if log:
                logger.error(f"Memcached error in {func.__name__}: {exc}")

            if hook and not hook(exc):
                raise
            return return_value

    return wrapper


class AsyncMemcachedLock:
    """
    Name-based reentrant distributed lock implementation using Memcached's add operation.

    Uses Memcached's atomic `add()` which only succeeds if key doesn't exist.
    This provides a lightweight distributed locking mechanism.
    """

    __slots__ = ("_name", "_client", "_release_timeout", "_acquired")

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
        :param release_timeout: Lock expiration time in seconds (default 10s).
        """
        self._name = name
        self._client = client
        self._release_timeout = (
            int(release_timeout) if release_timeout is not None else 5
        )
        self._acquired = False

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

        start_time = time()
        while True:
            # add() is atomic, as only succeeds if key doesn't exist
            # Returns True if added, False if key already exists
            added = await self._client.add(
                self._name.encode(),
                b"locked",
                exptime=self._release_timeout,
            )
            if added:
                self._acquired = True
                return True

            # Lock is held by someone else
            if not blocking:
                return False

            # Check timeout
            if blocking_timeout is not None:
                elapsed = time() - start_time
                if elapsed >= blocking_timeout:
                    return False

            # Wait a bit before retrying
            await asyncio.sleep(1e-9)

    async def release(self) -> None:
        """Release the lock."""
        if not self._acquired:
            raise RuntimeError(
                f"Cannot release lock '{self._name}' - not owned by this instance"
            )

        try:
            await self._client.delete(self._name.encode())
        except asyncio.CancelledError:
            raise
        except Exception:
            # Lock might have expired or been deleted
            pass
        finally:
            self._acquired = False

    async def __aenter__(self):
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire Memcached lock '{self._name}'")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class MemcachedBackend(ThrottleBackend[MemcachedClient, HTTPConnectionT]):
    """
    Memcached-based throttle backend with distributed locking support.

    Uses `aiomcache` for async Memcached operations.

    Note: Memcached has a key size limit of 250 bytes.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        host: str = "localhost",
        port: int = 11211,
        *,
        pool_size: int = 2,
        pool_minsize: int = 1,
        namespace: str = ":memcached:",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        """
        Initialize Memcached backend.

        :param host: Memcached server host.
        :param port: Memcached server port.
        :param namespace: Key prefix namespace.
        :param pool_size: Connection pool size.
        :param pool_minsize: Minimum pool size.
        """
        self.host = host
        self.port = port
        self.pool_size = pool_size
        self.pool_minsize = pool_minsize
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )

    def _get_tracking_key(self) -> str:
        """Get the key used to track all keys in this namespace."""
        return f"{self.namespace}:__tracked_keys__"

    async def _track_key(self, key: str) -> None:
        """Add key to tracking set."""
        if self.connection is None:
            return

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
                keys_set = set(tracked.decode().split("|"))
                if key not in keys_set:
                    keys_set.add(key)
                    new_tracked = "|".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
        except Exception as exc:
            logger.warning(f"Failed to track key '{key}': {exc}")

    async def _untrack_key(self, key: str) -> None:
        """Remove key from tracking set."""
        if self.connection is None:
            return

        tracking_key = self._get_tracking_key()
        try:
            tracked = await self.connection.get(tracking_key.encode())
            if tracked is None:
                return

            keys_set = set(tracked.decode().split("|"))
            if key in keys_set:
                keys_set.remove(key)
                if keys_set:
                    new_tracked = "|".join(sorted(keys_set))
                    await self.connection.set(
                        tracking_key.encode(),
                        new_tracked.encode(),
                        exptime=0,
                    )
                else:
                    await self.connection.delete(tracking_key.encode())
        except Exception as exc:
            logger.warning(f"Failed to untrack key '{key}': {exc}")

    async def initialize(self) -> None:
        """Initialize the Memcached connection."""
        if self.connection is None:
            self.connection = MemcachedClient(
                self.host,
                self.port,
                pool_size=self.pool_size,
                pool_minsize=self.pool_minsize,
            )

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
        return AsyncMemcachedLock(name, self.connection, release_timeout=10.0)

    async def get(self, key: str) -> typing.Optional[str]:
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
        await self._track_key(key)

    async def delete(self, key: str) -> bool:
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
        if deleted:
            await self._untrack_key(key)
        return deleted

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        Memcached's incr is atomic and thread-safe across all clients.

        :param key: Counter key.
        :param amount: Amount to increment by.
        :return: New value after increment.
        """
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        # Try to increment existing counter
        incr = _on_error_return(
            self.connection.incr,
            return_value=None,
            hook=lambda exc: b"NOT_FOUND" in str(exc).encode(),
        )
        new_value = await incr(key.encode(), amount)
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
            await self._track_key(key)
            return amount

        # Someone else created it, try increment again
        new_value = await self.connection.incr(key.encode(), amount)
        return typing.cast(int, new_value)

    async def decrement(self, key: str, amount: int = 1) -> int:
        """
        Atomically decrement counter.

        Memcached's decr is atomic and thread-safe across all clients.
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
        # If key doesn't exist, decr raises ClientException
        # We catch that and handle initialization below
        decr = _on_error_return(
            self.connection.decr,
            return_value=None,
            hook=lambda exc: b"NOT_FOUND" in str(exc).encode(),
        )
        new_value = await decr(key.encode(), amount)
        if new_value is not None:
            return new_value

        # Key doesn't exist, initialize it to 0 - amount
        # Use add() to atomically create if not exists
        added = await self.connection.add(
            key.encode(),
            str(0 - amount).encode(),
            exptime=0,
        )
        if added:
            await self._track_key(key)
            return 0 - amount

        # Someone else created it, try decrement again
        new_value = await self.connection.decr(key.encode(), amount)
        return typing.cast(int, new_value)

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

        For Memcached, we need to handle this specially since incr doesn't
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

        # Try to increment
        incr = _on_error_return(
            self.connection.incr,
            return_value=None,
            hook=lambda exc: b"NOT_FOUND" in str(exc).encode(),
        )
        new_value = await incr(key.encode(), amount)
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
            await self._track_key(key)
            return amount

        # Someone else created it, increment
        new_value = await self.connection.incr(key.encode(), amount)
        return typing.cast(int, new_value)

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get multiple keys.

        Uses Memcached's multi-get for efficiency.

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
        results = []
        for value in values:
            if value is not None:
                results.append(value.decode())
            else:
                results.append(None)
        return results

    async def clear(self) -> None:
        """Clear all tracked keys in the namespace."""
        if self.connection is None:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        tracking_key = self._get_tracking_key()
        tracked = await self.connection.get(tracking_key.encode())
        if tracked is None:
            return

        keys = tracked.decode().split("|")
        for key in keys:
            try:
                await self.connection.delete(key.encode())
            except Exception as exc:
                logger.warning(f"Failed to delete key '{key}': {exc}")

        await self.connection.delete(tracking_key.encode())

    async def reset(self) -> None:
        """Reset the backend."""
        await self.clear()

    async def close(self) -> None:
        """Close the Memcached connection."""
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
