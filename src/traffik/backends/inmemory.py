"""
In-memory implementation of a throttle backend using an `OrderedDict` for storage.

Note! This is not suitable for multi-process or distributed setups.
"""

import asyncio
import typing
from collections import OrderedDict
from time import monotonic
from types import TracebackType

from traffik._locks import (
    _AsyncFairRLock,
    _AsyncRLock,
    _NamedLockHandle,
    _NamedLockPool,
)
from traffik.backends.base import ThrottleBackend
from traffik.exceptions import BackendConnectionError, LockAcquisitionError
from traffik.typing import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    ThrottleErrorHandler,
)


class _AsyncLock(typing.Protocol):
    """Protocol for an underlying async lock used in the in-memory backend."""

    async def acquire(self) -> bool: ...
    def release(self) -> None: ...
    def is_owner(self, task: typing.Optional[asyncio.Task] = None) -> bool: ...
    def locked(self) -> bool: ...


class _AsyncInMemoryLock:
    """
    Async in-memory lock implementing the `AsyncLock` protocol.

    Non-reentrant by default but optionally reentrant per task.

    Reentrancy is delegated to the underlying lock implementation
    (`_AsyncFairRLock` or `_AsyncRLock`), both of which are inherently
    reentrant per task. When `reentrant=False`, re-acquisition attempts
    by the owning task are rejected at this wrapper level before reaching
    the underlying lock.
    """

    __slots__ = ("_lock", "_reentrant")

    def __init__(self, lock: _AsyncLock, reentrant: bool = False) -> None:
        """
        Initialize the lock.

        :param lock: The underlying async lock instance to wrap.
        :param reentrant: Whether to allow the same task to acquire the lock
            multiple times. When False, re-acquisition by the owning task
            raises `RuntimeError`. Defaults to False.
        """
        self._lock = lock
        self._reentrant = reentrant

    def locked(self) -> bool:
        """Return True if the lock is held by any task"""
        return self._lock.locked()

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        """Return True if the specified task (or current task if None) owns the lock."""
        return self._lock.is_owner(task=task)

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the lock.

        :param blocking: If False, return immediately if the lock is held by another task.
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :param blocking_timeout: Maximum time (seconds) to wait if blocking is True
            (Not supported as ops are in-memory and very fast).
            Only applicable to the initial acquire attempt, not reentrant attempts.
        :return: True if the lock was acquired, False otherwise.
        """
        current_task = asyncio.current_task()
        reentrant = self._lock.is_owner(task=current_task)
        if reentrant and not self._reentrant:
            raise LockAcquisitionError(
                "Lock is already acquired by the current task and was not configured as reentrant."
            )

        if not blocking:
            # If non-blocking and lock is held by another task, return False immediately
            if not reentrant and self._lock.locked():
                return False
            # Else, acquire the lock (reentrant or not held).
            # Delegate to underlying lock which handles the reentrancy too
            return await self._lock.acquire()

        # Delegate to underlying lock which handles the reentrancy too
        return await self._lock.acquire()

    async def release(self) -> None:
        """Release the lock."""
        self._lock.release()

    async def __aenter__(self):
        if not await self.acquire():
            raise LockAcquisitionError("Could not acquire inmemory lock.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ):
        await self.release()


class InMemoryBackend(ThrottleBackend[None, HTTPConnectionT]):
    """
    In-memory throttle backend.

    Uses shards (and hence lock striping) to improve concurrent access.

    Warning: Only use for development or single-worker applications.
    This will not work across multiple threads, processes, or servers.
    """

    wrap_methods = ("clear",)

    def __init__(
        self,
        namespace: str = "inmemory",
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        persistent: bool = False,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT, typing.Any]
        ] = None,
        on_error: typing.Union[
            typing.Literal["allow", "throttle", "raise"],
            ThrottleErrorHandler[HTTPConnectionT, typing.Mapping[str, typing.Any]],
        ] = "throttle",
        lock_blocking: typing.Optional[bool] = None,
        lock_ttl: typing.Optional[float] = None,
        lock_blocking_timeout: typing.Optional[float] = None,
        number_of_shards: int = 3,
        cleanup_frequency: typing.Optional[float] = None,
        lock_kind: typing.Literal["fair", "unfair"] = "unfair",
        lock_pool_size: int = 128,
        lock_pool_headroom: int = 4,
        prepopulate_lock_pool: bool = True,
        **kwargs: typing.Any,
    ) -> None:
        """
        Initialize the in-memory throttle backend.

        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
            Always non-persistent even when `persisent=True`.
        :param on_error: Strategy to handle errors during throttling operations.
            - "allow": Allow the request to proceed without throttling.
            - "throttle": Throttle the request as if it exceeded the rate limit.
            - "raise": Raise the exception encountered during throttling.
            - A custom callable that takes the connection and the exception as parameters
                and returns an integer representing the wait period in milliseconds. Ensure this
                function executes quickly to avoid additional latency.
        :param lock_blocking: Whether locks should block when acquiring.
            If None, uses the global default from `traffik.config.get_lock_blocking()`.
        :param lock_ttl: Default TTL for locks in seconds. If None, locks have
            no expiration unless specified during lock acquisition.
        :param lock_blocking_timeout: Default maximum time to wait for acquiring locks in seconds.
            If None, uses the global default from `traffik.config.get_lock_blocking_timeout()`.
        :param number_of_shards: Number of shards to split the in-memory shard into for concurrency.
        :param cleanup_frequency: Frequency (in seconds) to cleanup expired keys. If None, no automatic cleanup is performed.
        :param lock_kind: The type of lock to use for shard locks. "fair" uses a fair lock implementation which
            guarantees FIFO order for waiting tasks, while "unfair" may have better performance but does not guarantee order.
        :param lock_pool_size: Maximum number of idle named locks to keep in the pool for reuse.
            When the pool is exhausted, new locks will be created on demand.
        :param lock_pool_headroom: The headroom multiplier for the named lock pool.
            When the number of idle locks in the pool exceeds `lock_pool_size * lock_pool_headroom`,
            the excess locks will be closed to free up resources.
            This allows the pool to temporarily grow under high contention while still enforcing an upper
            bound on resource usage.
        :param prepopulate_lock_pool: Whether to pre-populate the named lock pool with idle locks up to `lock_pool_size` on initialization.
             Pre-populating can reduce latency for the first few lock acquisitions at the cost of using more resources upfront.
        :param kwargs: Additional keyword arguments.
        """
        kwargs.pop("persistent", None)
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

        self._number_of_shards = number_of_shards
        self._shard_locks: typing.List[asyncio.Lock] = []
        """Locks for each shard to allow concurrent access."""
        self._shards: typing.List[OrderedDict[str, typing.Any]] = []
        """In-memory storage shards."""

        self._lock_cls = _AsyncFairRLock if lock_kind == "fair" else _AsyncRLock
        self._lock_pool_size = lock_pool_size
        self._lock_pool_headroom = lock_pool_headroom
        self._reentrant_lock_pool: typing.Optional[
            _NamedLockPool[_AsyncInMemoryLock]
        ] = None
        self._non_reentrant_lock_pool: typing.Optional[
            _NamedLockPool[_AsyncInMemoryLock]
        ] = None
        self._prepopulate_lock_pool = prepopulate_lock_pool

        self._cleanup_task: typing.Optional[asyncio.Task] = None
        self._cleanup_frequency = cleanup_frequency
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the in-memory storage."""
        if self._initialized:
            return

        if not self._shard_locks:
            self._shard_locks = [asyncio.Lock() for _ in range(self._number_of_shards)]
        if not self._shards:
            self._shards = [OrderedDict() for _ in range(self._number_of_shards)]

        if self._reentrant_lock_pool is None or self._reentrant_lock_pool.closed:
            self._reentrant_lock_pool = _NamedLockPool(
                factory=lambda: _AsyncInMemoryLock(
                    lock=self._lock_cls(), reentrant=True
                ),
                max_size=self._lock_pool_size,
                headroom=self._lock_pool_headroom,
            )

        if (
            self._non_reentrant_lock_pool is None
            or self._non_reentrant_lock_pool.closed
        ):
            self._non_reentrant_lock_pool = _NamedLockPool(
                factory=lambda: _AsyncInMemoryLock(
                    lock=self._lock_cls(), reentrant=False
                ),
                max_size=self._lock_pool_size,
                headroom=self._lock_pool_headroom,
            )

        if self._prepopulate_lock_pool:
            self._reentrant_lock_pool.populate()
            self._non_reentrant_lock_pool.populate()

        if self._cleanup_task is None and self._cleanup_frequency:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._initialized = True

    async def ready(self) -> bool:
        return self._initialized and bool(self._shards)

    def _assert_ready(self) -> None:
        """
        Raise `BackendConnectionError` if the backend has not been initialized.
        """
        if not self._initialized or not self._shards:
            raise BackendConnectionError(
                "Connection error! Ensure backend is initialized."
            )

    def _get_shard(self, key: str) -> typing.Tuple[int, asyncio.Lock, OrderedDict]:
        """Get shard index, lock, and shard for a key."""
        shard_idx = hash(key) % self._number_of_shards
        return shard_idx, self._shard_locks[shard_idx], self._shards[shard_idx]

    async def keys(self) -> typing.List[str]:
        """Get all keys in the backend."""
        self._assert_ready()

        all_keys = []
        # Acquire all shard locks in order
        for lock, shard in zip(self._shard_locks, self._shards):
            async with lock:
                all_keys.extend(list(shard.keys()))
        return all_keys

    async def _cleanup(self) -> None:
        """Remove expired keys from all shards."""
        now = monotonic()

        # Clean each shard independently
        for lock, shard in zip(self._shard_locks, self._shards):
            async with lock:
                expired = [
                    key
                    for key, (_, expires_at) in shard.items()
                    if expires_at is not None and expires_at <= now
                ]
                for key in expired:
                    del shard[key]

    async def _cleanup_loop(self) -> None:
        """Periodically reclaim expired entries. Runs as a background `asyncio.Task`."""
        assert self._cleanup_frequency
        while self._initialized:
            await asyncio.sleep(self._cleanup_frequency)
            await self._cleanup()

    def get_lock(
        self, name: str, ttl: typing.Optional[float] = None, reentrant: bool = False
    ) -> _NamedLockHandle[_AsyncInMemoryLock]:
        """
        Return a lock for the given name.

        This is meant for user-requested locks (e.g., strategy locking, multi-key operations).
        The locks are managed in a pool to allow reuse and limit resource usage.

        :param name: The name of the lock. This should be a unique identifier for the resource being locked.
        :param ttl: Optional TTL for the lock in seconds. If specified, the lock will
            automatically expire after the TTL if not released. If None, locks have no expiration.
        :param reentrant: Whether the lock should allow reentrancy by the same task.
            If True, the same task can acquire the lock multiple times without causing a deadlock.
            If False, re-acquisition by the owning task will raise a `LockAcquisitionError`.
            Defaults to False.
        :return: A `_NamedLockHandle` for the requested lock.
        """
        self._assert_ready()
        return (
            self._reentrant_lock_pool.get(name)  # type: ignore[union-attr]
            if reentrant
            else self._non_reentrant_lock_pool.get(name)  # type: ignore[union-attr]
        )

    def lock(
        self,
        name: str,
        ttl: typing.Optional[float] = None,
        blocking: typing.Optional[bool] = None,
        blocking_timeout: typing.Optional[float] = None,
        reentrant: bool = False,
        # Default to True to enforce TTL locally in the backend since locks are in-memory and local.
        enforce_ttl_locally: bool = True,
        local_ttl_factor: float = 1.0,
    ):
        return super().lock(
            name=name,
            ttl=ttl,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
            reentrant=reentrant,
            enforce_ttl_locally=enforce_ttl_locally,
            local_ttl_factor=local_ttl_factor,
        )

    # Note: Shard locks are not essentially needed in the `get`, `set`, `delete`,
    # `increment`, etc. methods (except in `multi_set` and `multi_get`). This because shard ops
    # are essentially atomic since we have no `await` statements in the code blocks
    # We just add them for semantic clarity (to show that said block is meant to be atomic)
    # and future proofing. The lock overhead should not be significant.

    async def get(
        self, key: str, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Optional[str]:
        """Get value by key."""
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        entry = shard.get(key)
        if entry is None:
            return None

        value, expires_at = entry
        # Check if expired
        if expires_at is not None and expires_at <= monotonic():
            async with lock:
                del shard[key]
            return None
        return value

    async def set(
        self, key: str, value: str, expire: typing.Optional[float] = None
    ) -> None:
        """Set value by key."""
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        expires_at = None
        if expire is not None:
            expires_at = monotonic() + expire

        async with lock:
            shard[key] = (value, expires_at)

    async def delete(self, key: str, *args: typing.Any, **kwargs: typing.Any) -> bool:
        """Delete key if exists."""
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        async with lock:
            if key in shard:
                del shard[key]
                return True
            return False

    async def increment(self, key: str, amount: int = 1) -> int:
        """
        Atomically increment counter.

        :param key: Counter key
        :param amount: Amount to increment by
        :return: New value after increment
        """
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        async with lock:
            entry = shard.get(key)
            if entry is None:
                # Key doesn't exist, initialize
                shard[key] = (str(amount), None)
                return amount

            value, expires_at = entry
            # Check if expired
            if expires_at is not None and expires_at <= monotonic():
                # Expired, reinitialize
                shard[key] = (str(amount), None)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset
                shard[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            shard[key] = (str(new_value), expires_at)
            return new_value

    async def expire(self, key: str, seconds: int) -> bool:
        """
        Set expiration on existing key.

        :param key: Key to set expiration on
        :param seconds: TTL in seconds
        :return: True if expiration was set, False if key doesn't exist
        """
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        async with lock:
            entry = shard.get(key)
            if entry is None:
                return False

            value, _ = entry
            expires_at = monotonic() + seconds
            shard[key] = (value, expires_at)
            return True

    async def increment_with_ttl(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        """
        Atomic increment + TTL in single operation.

        :param key: Counter key
        :param amount: Amount to increment
        :param ttl: TTL to set if key is new (seconds)
        :return: New value after increment
        """
        self._assert_ready()

        _, lock, shard = self._get_shard(key)
        now = monotonic()
        async with lock:
            entry = shard.get(key)
            if entry is None:
                # New key, initialize with TTL
                expires_at = now + ttl
                shard[key] = (str(amount), expires_at)
                return amount

            value, expires_at = entry  # type: ignore[assignment]
            # Check if expired
            if expires_at is not None and expires_at <= now:
                # Expired, reinitialize with new TTL
                expires_at = now + ttl
                shard[key] = (str(amount), expires_at)
                return amount

            # Increment existing value
            try:
                current = int(value)
            except (ValueError, TypeError):
                # Invalid value, reset with TTL
                expires_at = now + ttl
                shard[key] = (str(amount), expires_at)
                return amount

            new_value = current + amount
            # Only set TTL if key was created without expiration
            # (e.g., via increment() call, not increment_with_ttl)
            if expires_at is None:
                expires_at = now + ttl

            shard[key] = (str(new_value), expires_at)
            return new_value

    async def multi_get(self, *keys: str) -> typing.List[typing.Optional[str]]:
        """
        Batch get all values retrieved atomically.

        :param keys: List of keys to get
        :return: List of values (None for missing keys), same order as keys
        """
        self._assert_ready()
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
        now = monotonic()

        for shard_idx in sorted(shard_keys.keys()):
            lock = self._shard_locks[shard_idx]
            shard = self._shards[shard_idx]

            async with lock:
                for key in shard_keys[shard_idx]:
                    entry = shard.get(key)
                    if entry is None:
                        results[key] = None
                        continue

                    value, expires_at = entry
                    if expires_at is None or expires_at > now:
                        results[key] = value
                    else:
                        del shard[key]
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
        self._assert_ready()
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
        expires_at = monotonic() + expire if expire is not None else None

        # Acquire locks in sorted order to prevent deadlocks
        for shard_idx in sorted(shard_items.keys()):
            lock = self._shard_locks[shard_idx]
            shard = self._shards[shard_idx]

            async with lock:
                for key, value in shard_items[shard_idx]:
                    shard[key] = (value, expires_at)

    async def clear(self) -> None:
        """Clear all keys in the namespace."""
        self._assert_ready()

        # Acquire all shard locks in order
        for lock, shard in zip(self._shard_locks, self._shards):
            async with lock:
                # All keys in the bacens shard should be in the backend's
                # namespace already so just clear the whole shard
                shard.clear()

    async def reset(self) -> None:
        """Reset the backend by clearing all data."""
        # Just clear all data since this is an in-memory backend.
        await self.clear()

    def closed(self) -> bool:
        """Return True if the backend is closed."""
        return not self._initialized

    async def close(self) -> None:
        """
        Shut down this backend instance.

        Cancels the cleanup task, and closes the named lock pool.
        """
        if not self._initialized:
            return

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

        if self._reentrant_lock_pool is not None:
            self._reentrant_lock_pool.close()
            self._reentrant_lock_pool = None

        if self._non_reentrant_lock_pool is not None:
            self._non_reentrant_lock_pool.close()
            self._non_reentrant_lock_pool = None

        # TODO: May need to delete shards and shard locks here.
        # But that conflict with th role of the `close` method
        # as defined on the base class, which is to close client
        # connections which this backend doesnt have (not clear the
        # backend data which is to be done by `clear`/`reset`)
