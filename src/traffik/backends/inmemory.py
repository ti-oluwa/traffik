import asyncio
from collections import OrderedDict
import typing

from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
)
from traffik.utils import AsyncRLock, time


class AsyncInMemoryLock:
    """Lock for in-memory backend."""

    def __init__(self) -> None:
        self._lock = AsyncRLock()

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the lock.

        :param blocking: If False, return immediately if the lock is held.
        :param blocking_timeout: Max time (seconds) to wait if blocking is True.
        :return: True if the lock was acquired, False otherwise.
        """
        if not blocking:
            # Non-blocking. Try to acquire immediately
            # Use asyncio.Lock's locked() in `AsyncRLock` instance which is atomic
            if (
                self._lock._lock.locked()
                and self._lock._owner != asyncio.current_task()
            ):
                return False
            return await self._lock.acquire()

        if blocking_timeout is None:
            # Normal blocking acquire (no timeout)
            return await self._lock.acquire()

        try:
            # Blocking with timeout
            await asyncio.wait_for(self._lock.acquire(), timeout=blocking_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def release(self) -> None:
        """Release the lock."""
        self._lock.release()


class InMemoryBackend(
    ThrottleBackend[
        typing.Optional[
            typing.MutableMapping[
                str,
                typing.Tuple[str, typing.Optional[float]],
            ]
        ],
        HTTPConnectionT,
    ]
):
    """
    In-memory throttle backend with proper atomic operations support.

    Uses a SINGLE global lock to protect ALL operations on the shared storage.
    This ensures true atomicity and prevents race conditions.

    Storage format: key -> (value: str, expires_at: Optional[float])

    Warning: Only use for testing or single-process applications.
    Does not work across multiple processes/servers.
    """
    wrap_methods = ("clear",)

    def __init__(
        self,
        namespace: str = "inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
        )
        # Global lock for all storage operations
        self._global_lock = AsyncRLock()

    async def initialize(self) -> None:
        """Initialize the in-memory storage."""
        if self.connection is None:
            # Use `OrderedDict` to maintain insertion order (for predictable expiration cleanup)
            self.connection = OrderedDict()

    async def _cleanup_expired(self) -> None:
        """
        Remove expired keys.

        MUST be called with `_global_lock` held!
        """
        if self.connection is None:
            return

        now = time()
        expired = [
            key
            for key, (_, expires_at) in list(self.connection.items())
            if expires_at is not None and expires_at < now
        ]
        for key in expired:
            del self.connection[key]

    async def get_lock(self, name: str) -> AsyncInMemoryLock:
        """
        Returns a lock for the given name.

        Note: For InMemory backend, all operations use the global lock internally,
        so this is mainly for API compatibility.
        """
        # Return a wrapper around the global lock
        lock = AsyncInMemoryLock()
        lock._lock = self._global_lock
        return lock

    async def get(self, key: str) -> typing.Optional[str]:
        """Get value by key."""
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            await self._cleanup_expired()
            entry = self.connection.get(key)
            if entry is None:
                return None
            
            value, expires_at = entry
            # Check if expired
            if expires_at is not None and expires_at < time():
                del self.connection[key]
                return None
            return value

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """Set value by key."""
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            expires_at = None
            if expire is not None:
                expires_at = time() + expire
            self.connection[key] = (value, expires_at)

    async def delete(self, key: str) -> bool:
        """Delete key if exists."""
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            if key in self.connection:
                del self.connection[key]
                return True
            return False

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        :param key: Counter key
        :param amount: Amount to increment by
        :return: New value after increment
        """
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            await self._cleanup_expired()

            entry = self.connection.get(key)
            if entry is None:
                # Key doesn't exist, initialize
                self.connection[key] = (str(amount), None)
                return amount

            value, expires_at = entry
            # Check if expired
            if expires_at is not None and expires_at < time():
                # Expired, reinitialize
                self.connection[key] = (str(amount), None)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset
                self.connection[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            self.connection[key] = (str(new_value), expires_at)
            return new_value

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        :param key: Key to set expiration on
        :param seconds: TTL in seconds
        :return: True if expiration was set, False if key doesn't exist
        """
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            entry = self.connection.get(key)

            if entry is None:
                return False

            value, _ = entry
            expires_at = time() + seconds
            self.connection[key] = (value, expires_at)
            return True

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomic increment + TTL in single operation.

        :param key: Counter key
        :param amount: Amount to increment
        :param ttl: TTL to set if key is new (seconds)
        :return: New value after increment
        """
        if self.connection is None:
            raise BackendError("Backend not initialized")

        async with self._global_lock:
            await self._cleanup_expired()

            entry = self.connection.get(key)
            if entry is None:
                # New key, initialize with TTL
                expires_at = time() + ttl
                self.connection[key] = (str(amount), expires_at)
                return amount

            value, expires_at = entry
            # Check if expired
            if expires_at is not None and expires_at < time():
                # Expired, reinitialize with new TTL
                expires_at = time() + ttl
                self.connection[key] = (str(amount), expires_at)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset with TTL
                expires_at = time() + ttl
                self.connection[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            # Only set TTL if key was created without expiration
            # (e.g., via increment() call, not increment_with_ttl)
            if expires_at is None:
                expires_at = time() + ttl

            self.connection[key] = (str(new_value), expires_at)
            return new_value

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get - all values retrieved atomically.

        :param keys: List of keys to get
        :return: List of values (None for missing keys), same order as keys
        """
        if self.connection is None:
            raise BackendError("Backend not initialized")

        if not keys:
            return []

        async with self._global_lock:
            await self._cleanup_expired()

            now = time()
            results = []

            for key in keys:
                entry = self.connection.get(key)
                if entry is None:
                    results.append(None)
                    continue

                value, expires_at = entry
                if expires_at is None or expires_at > now:
                    results.append(value)
                else:
                    # Expired, remove and return None
                    del self.connection[key]
                    results.append(None)
            return results

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        if self.connection is None:
            return

        async with self._global_lock:
            keys_to_delete = [
                key
                for key in list(self.connection.keys())
                if key.startswith(f"{self.namespace}:")
            ]
            for key in keys_to_delete:
                del self.connection[key]

    async def reset(self) -> None:
        """Reset the backend by clearing all data."""
        async with self._global_lock:
            if self.connection is not None:
                self.connection.clear()
            self.connection = None

    async def close(self) -> None:
        """Close the backend (no-op for in-memory)."""
        pass
