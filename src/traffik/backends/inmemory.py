"""
In-memory implementation of a throttle backend using an `OrderedDict` for storage.

Note! This is not suitable for multi-process or distributed setups.
"""

import asyncio
import typing
from collections import OrderedDict

from traffik._locks import _AsyncFairRLock, _AsyncRLock
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)
from traffik.utils import time


class _AsyncLock(typing.Protocol):
    async def acquire(self) -> bool: ...
    def release(self) -> None: ...
    def is_owner(self, task: typing.Optional[asyncio.Task] = None) -> bool: ...
    def locked(self) -> bool: ...


class _AsyncInMemoryLock:
    """Re-entrant async (un-fair) lock for in-memory backend."""

    __slots__ = "_lock"

    def __init__(self, lock: _AsyncLock) -> None:
        self._lock = lock

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
        :param blocking_timeout: Max time (seconds) to wait if blocking is True (Not supported).
        :return: True if the lock was acquired, False otherwise.
        """
        if not blocking:
            current_task = asyncio.current_task()
            # Non-blocking. Try to acquire immediately
            if self._lock.locked() and not self._lock.is_owner(task=current_task):
                return False
            return await self._lock.acquire()

        return await self._lock.acquire()

        # This segment has issues
        # if blocking_timeout is None:
        #     # Normal blocking acquire (no timeout)
        #     return await self._lock.acquire()

        # # Check if this is a reentrant acquisition
        # current_task = asyncio.current_task()
        # if self._lock.is_owner(task=current_task):
        #     # Acquire lock without timeout to avoid cancellation issues
        #     return await self._lock.acquire()

        # # Blocking with timeout. Can't use `asyncio.wait_for` as `_AsyncRLock.acquire()`
        # # cancellation on timeout leads to a corrupted state in the lock.
        # # Hence we use a separate timeout task.
        # acquire_task = asyncio.create_task(self._lock.acquire())
        # timeout_task = asyncio.create_task(asyncio.sleep(blocking_timeout))

        # done, _ = await asyncio.wait(
        #     {acquire_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
        # )
        # if acquire_task in done:
        #     # Lock acquired successfully
        #     timeout_task.cancel()
        #     try:
        #         await timeout_task
        #     except asyncio.CancelledError:
        #         pass
        #     return True

        # # Timeout occurred. Cancel the acquire task
        # acquire_task.cancel()
        # try:
        #     await acquire_task
        # except asyncio.CancelledError:
        #     # Lock acquisition was cancelled, but it might have succeeded
        #     # just before cancellation. Check if we got it.
        #     if self._lock.is_owner(task=current_task):
        #         # We got the lock right before timeout, we keep it
        #         return True
        # return False

    async def release(self) -> None:
        """Release the lock."""
        self._lock.release()


class InMemoryBackend(ThrottleBackend[None, HTTPConnectionT]):
    """
    In-memory throttle backend.

    Uses shards (and hence lock striping) to improve concurrent access.

    Warning: Only use for development or single-process applications.
    Does not work across multiple processes/servers.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        namespace: str = "inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, typing.Any]
        ] = None,
        persistent: bool = False,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        lock_blocking: typing.Optional[bool] = None,
        lock_ttl: typing.Optional[float] = None,
        lock_blocking_timeout: typing.Optional[float] = None,
        number_of_shards: int = 3,
        cleanup_frequency: typing.Optional[float] = 10.0,
        lock_kind: typing.Literal["fair", "unfair"] = "unfair",
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the in-memory throttle backend.

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
        :param lock_blocking: Whether locks should block when acquiring.
            If None, uses the global default from `traffik.utils.get_lock_blocking()`.
        :param lock_ttl: Default TTL for locks in seconds. If None, locks have
            no expiration unless specified during lock acquisition.
        :param lock_blocking_timeout: Default maximum time to wait for acquiring locks in seconds.
            If None, uses the global default from `traffik.utils.get_lock_blocking_timeout()`.
        :param number_of_shards: Number of shards to split the in-memory store into for concurrency.
        :param cleanup_frequency: Frequency (in seconds) to cleanup expired keys. If None, no automatic cleanup is performed.
        :param kwargs: Additional keyword arguments.
        """
        super().__init__(
            None,
            namespace=namespace,
            identifier=identifier,
            handle_throttled=handle_throttled,
            persistent=persistent,
            on_error=on_error,
            lock_blocking=lock_blocking,
            lock_ttl=lock_ttl,
            lock_blocking_timeout=lock_blocking_timeout,
            **kwargs,
        )
        if number_of_shards < 1:
            raise ValueError("`number_of_shards` must be at least 1")

        self._num_shards = number_of_shards
        self._shard_locks: typing.List[asyncio.Lock] = []
        """Locks for each shard to allow concurrent access."""
        self._shard_stores: typing.List[OrderedDict] = []
        """In-memory storage shards."""

        self._lock_cls = _AsyncFairRLock if lock_kind == "fair" else _AsyncRLock
        # Separate registry for user-requested named locks
        self._named_locks: typing.Dict[str, _AsyncInMemoryLock] = {}
        """Lock registry for named locks requested by users."""
        self._named_locks_lock = self._lock_cls()
        """Lock to protect access to the named locks registry."""

        self._cleanup_task: typing.Optional[asyncio.Task] = None
        self._cleanup_frequency = cleanup_frequency
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the in-memory storage."""
        if self._initialized:
            return

        if not self._shard_locks:
            self._shard_locks = [asyncio.Lock() for _ in range(self._num_shards)]
        if not self._shard_stores:
            self._shard_stores = [OrderedDict() for _ in range(self._num_shards)]

        if self._cleanup_task is None and self._cleanup_frequency:
            self._cleanup_task = await self._start_cleanup_task(
                frequency=self._cleanup_frequency
            )
        self._initialized = True

    async def ready(self) -> bool:
        return self._initialized

    def _get_shard(self, key: str) -> typing.Tuple[int, asyncio.Lock, OrderedDict]:
        """Get shard index, lock, and store for a key."""
        shard_idx = hash(key) % self._num_shards
        return shard_idx, self._shard_locks[shard_idx], self._shard_stores[shard_idx]

    async def keys(self) -> typing.List[str]:
        """Get all keys in the backend."""
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        all_keys = []
        # Acquire all shard locks in order
        for lock, store in zip(self._shard_locks, self._shard_stores):
            async with lock:
                all_keys.extend(list(store.keys()))
        return all_keys

    async def _cleanup_expired(self) -> None:
        """Remove expired keys from all shards."""
        now = time()

        # Clean each shard independently
        for lock, store in zip(self._shard_locks, self._shard_stores):
            async with lock:
                expired = [
                    key
                    for key, (_, expires_at) in list(store.items())
                    if expires_at is not None and expires_at < now
                ]
                for key in expired:
                    del store[key]

    async def _start_cleanup_task(self, frequency: float = 0.1) -> asyncio.Task[None]:
        """
        Start background task to cleanup expired keys.

        :param frequency: Cleanup interval in seconds.
        :return: The created asyncio Task.
        """

        async def _cleanup_task():
            while self._initialized:
                await asyncio.sleep(frequency)  # Cleanup interval
                await self._cleanup_expired()

        return asyncio.create_task(_cleanup_task())

    async def get_lock(self, name: str) -> _AsyncInMemoryLock:
        """
        Returns a reentrant lock for the given name.

        This is for user-requested locks (e.g., strategy locking, multi-key operations).
        Internal shard locks are separate and automatic.
        """
        named_locks = self._named_locks
        if name in named_locks:
            return named_locks[name]

        async with self._named_locks_lock:
            lock = _AsyncInMemoryLock(lock=self._lock_cls())
            named_locks[name] = lock
        return lock

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Get value by key."""
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        entry = store.get(key)
        if entry is None:
            return None

        value, expires_at = entry
        # Check if expired
        if expires_at is not None and expires_at < time():
            return None
        return value

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """Set value by key."""
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        expires_at = None
        if expire is not None:
            expires_at = time() + expire

        async with lock:
            store[key] = (value, expires_at)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """Delete key if exists."""
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        async with lock:
            if key in store:
                del store[key]
                return True
            return False

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        :param key: Counter key
        :param amount: Amount to increment by
        :return: New value after increment
        """
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        async with lock:
            entry = store.get(key)
            if entry is None:
                # Key doesn't exist, initialize
                store[key] = (str(amount), None)
                return amount

            value, expires_at = entry
            # Check if expired
            if expires_at is not None and expires_at < time():
                # Expired, reinitialize
                store[key] = (str(amount), None)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset
                store[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            store[key] = (str(new_value), expires_at)
            return new_value

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        :param key: Key to set expiration on
        :param seconds: TTL in seconds
        :return: True if expiration was set, False if key doesn't exist
        """
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        async with lock:
            entry = store.get(key)
            if entry is None:
                return False

            value, _ = entry
            expires_at = time() + seconds
            store[key] = (value, expires_at)
            return True

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomic increment + TTL in single operation.

        :param key: Counter key
        :param amount: Amount to increment
        :param ttl: TTL to set if key is new (seconds)
        :return: New value after increment
        """
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        _, lock, store = self._get_shard(key)
        async with lock:
            entry = store.get(key)
            if entry is None:
                # New key, initialize with TTL
                expires_at = time() + ttl
                store[key] = (str(amount), expires_at)
                return amount

            value, expires_at = entry  # type: ignore[assignment]
            # Check if expired
            if expires_at is not None and expires_at < time():
                # Expired, reinitialize with new TTL
                expires_at = time() + ttl
                store[key] = (str(amount), expires_at)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset with TTL
                expires_at = time() + ttl
                store[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            # Only set TTL if key was created without expiration
            # (e.g., via increment() call, not increment_with_ttl)
            if expires_at is None:
                expires_at = time() + ttl

            store[key] = (str(new_value), expires_at)
            return new_value

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get all values retrieved atomically.

        :param keys: List of keys to get
        :return: List of values (None for missing keys), same order as keys
        """
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not keys:
            return []

        # Group keys by shard
        shard_keys: typing.Dict[int, typing.List[str]] = {}
        key_to_shard: typing.Dict[str, int] = {}

        for key in keys:
            shard_idx, _, _ = self._get_shard(key)
            key_to_shard[key] = shard_idx
            if shard_idx not in shard_keys:
                shard_keys[shard_idx] = []
            shard_keys[shard_idx].append(key)

        # Acquire locks in sorted order to prevent deadlocks
        results: typing.Dict[str, typing.Optional[str]] = {}
        now = time()

        for shard_idx in sorted(shard_keys.keys()):
            lock = self._shard_locks[shard_idx]
            store = self._shard_stores[shard_idx]

            async with lock:
                for key in shard_keys[shard_idx]:
                    entry = store.get(key)
                    if entry is None:
                        results[key] = None
                        continue

                    value, expires_at = entry
                    if expires_at is None or expires_at > now:
                        results[key] = value
                    else:
                        del store[key]
                        results[key] = None

        # Return results in original key order
        return [results[key] for key in keys]

    async def multi_set(
        self,
        items: typing.Mapping[str, str],
        expire: typing.Optional[int] = None,
    ) -> None:
        """
        Batch set multiple values atomically.

        Groups keys by shard and acquires locks in sorted order to prevent deadlocks.

        :param items: Mapping of keys to values
        :param expire: Optional TTL in seconds for all keys
        """
        if not self._initialized:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

        if not items:
            return

        # Group items by shard
        shard_items: typing.Dict[int, typing.List[typing.Tuple[str, str]]] = {}
        for key, value in items.items():
            shard_idx, _, _ = self._get_shard(key)
            if shard_idx not in shard_items:
                shard_items[shard_idx] = []
            shard_items[shard_idx].append((key, value))

        # Calculate expiration time once
        expires_at = time() + expire if expire is not None else None

        # Acquire locks in sorted order to prevent deadlocks
        for shard_idx in sorted(shard_items.keys()):
            lock = self._shard_locks[shard_idx]
            store = self._shard_stores[shard_idx]

            async with lock:
                for key, value in shard_items[shard_idx]:
                    store[key] = (value, expires_at)

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        if not self._initialized:
            return

        # Acquire all shard locks in order
        for lock, store in zip(self._shard_locks, self._shard_stores):
            async with lock:
                keys_to_delete = [
                    key
                    for key in list(store.keys())
                    if key.startswith(f"{self.namespace}:")
                ]
                for key in keys_to_delete:
                    del store[key]

    async def reset(self) -> None:
        """Reset the backend by clearing all data."""
        if not self._initialized:
            return

        # Acquire all shard locks in order
        for lock, store in zip(self._shard_locks, self._shard_stores):
            async with lock:
                store.clear()

    async def close(self) -> None:
        """Close the backend."""
        # Clear all keys
        await self.clear()
        self._initialized = False

        # Stop cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
            self._cleanup_task = None

        # Clear named locks
        async with self._named_locks_lock:
            self._named_locks.clear()
