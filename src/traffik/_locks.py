"""Locking utilities"""

import asyncio
import sys
import threading
import typing
from collections import deque
from contextlib import nullcontext
from time import time_ns
from types import TracebackType

from typing_extensions import Self

from traffik.exceptions import LockTimeoutError
from traffik.types import AsyncLock, T


class _FenceTokenGenerator:
    """
    A thread-safe fence token generator that produces unique, monotonically increasing integer tokens.

    Each token is a combination of the current timestamp in nanoseconds and a sequence number
    to ensure uniqueness even when multiple tokens are generated within the same nanosecond.

    64-bit Token Structure:
    - Upper 48 bits: Timestamp in nanoseconds.
    - Lower 16 bits: Sequence number (0-65535).
    """

    __slots__ = ("_lock", "_last_timestamp", "_sequence_number")

    def __init__(self):
        self._lock = threading.Lock()
        """Lock for thread-safe token generation."""
        self._last_timestamp = 0
        """Last timestamp in nanoseconds."""
        self._sequence_number = 0
        """Sequence number for tokens generated within the same nanosecond."""

    def next(self) -> int:
        """Generate the next unique fence token."""
        with self._lock:
            timestamp = int(time_ns())

            if timestamp == self._last_timestamp:
                self._sequence_number += 1
            else:
                self._last_timestamp = timestamp
                self._sequence_number = 0

            return (timestamp << 16) | self._sequence_number


fence_token_generator = _FenceTokenGenerator()
"""Global fence token generator instance."""


AsyncLockT = typing.TypeVar("AsyncLockT", bound=AsyncLock)


class _NamedLockPool(typing.Generic[AsyncLockT]):
    """
    A pool of reusable named locks.

    Each unique name maps to a single underlying lock instance plus a
    reference count. When the count drops to zero (no task is waiting or
    holding the lock) the lock is returned to the free list and the name
    is removed from the active mapping, making the memory reusable.

    When the free list is empty a new lock is created via *factory*,
    so `max_size` is a **soft cap on the free list**, not a hard cap
    on total concurrency. Locks returned to an already-full free list are
    simply discarded.

    Note: This is not thread-safe by default. Pass `threadsafe=True` for additional
    safety guards.

    Usage:

    ```python

    pool = _NamedLockPool(
        factory=lambda: _AsyncInMemoryLock(_AsyncRLock()),
        max_size=128,
    )

    handle = pool.get("some:key")
    async with handle:
        ...  # exclusive section
    ```
    """

    __slots__ = ("_factory", "_max_size", "_free", "_active", "_proxy", "_guard")

    def __init__(
        self,
        factory: typing.Callable[[], typing.Union[AsyncLockT, T]],
        max_size: int = 128,
        threadsafe: bool = False,
        proxy: typing.Optional[
            typing.Callable[[typing.Union[AsyncLockT, T]], AsyncLockT]
        ] = None,
    ) -> None:
        """
        Initialize the pool

        :param factory: Zero-argument callable that produces a fresh lock instance or an object that
            will be wrapped by `proxy`. Called when the free list is empty and a new lock is needed.
        :param max_size: Maximum number of idle locks kept in the free list (soft cap). Defaults to 128.
            Locks exceeding this limit are discarded.
        :param threadsafe: If True, uses thread-safety guards (lock) for concurrent access to the pool.
            Defaults to False.
        :param proxy: Optional callable that wraps the factory-returned instance retrieved from the pool.
            Takes the instance as argument and returns the wrapped as an `AsyncLock`. Defaults to None.
        """
        self._factory = factory
        self._max_size = max_size
        self._proxy = proxy
        self._free: typing.List[typing.Union[AsyncLockT, typing.Any]] = []
        """Idle locks available for reuse."""
        self._active: typing.Dict[
            str, typing.Tuple[typing.Union[AsyncLockT, typing.Any], int]
        ] = {}
        """Mapping of `name` to `(lock, refcount)` for all currently referenced locks."""
        self._guard = threading.Lock() if threadsafe else nullcontext()

    def get(self, name: str, /) -> "_NamedLockHandle[AsyncLockT]":
        """
        Return a handle for the given `name`, incrementing its reference count.

        If `name` is already active the same underlying lock is reused
        (so concurrent tasks on the same name actually contend on one lock,
        which is the desired behaviour). If `name` is new, a lock is taken
        from the free list or created fresh.

        :param name: The logical name identifying the lock.
        :return: A `_NamedLockHandle` wrapping the lock for `name`.
        """
        if self._proxy:
            return _NamedLockHandle(
                pool=self,
                name=name,
                lock=self._proxy(self._increment_ref_or_create(name)),
            )
        return _NamedLockHandle(
            pool=self,
            name=name,
            lock=self._increment_ref_or_create(name),
        )

    def _increment_ref_or_create(
        self, name: str, /
    ) -> typing.Union[AsyncLockT, typing.Any]:
        """
        Increment the reference count for `name` and return its lock,
        or allocate a fresh lock when `name` is not yet active.

        :param name: Lock name.
        :return: The lock instance associated with `name`.
        """
        with self._guard:
            entry = self._active.get(name)
            if entry is not None:
                lock, count = entry
                self._active[name] = (lock, count + 1)
                return lock

            # Allocate from free list or factory.
            lock = self._free.pop() if self._free else self._factory()
            self._active[name] = (lock, 1)
            return lock

    def _decrement_ref(self, name: str, /) -> None:
        """
        Decrement the reference count for `name`.

        When the count reaches zero the name is removed from the active
        mapping and the lock is returned to the free list (if space remains) or discarded.

        :param name: Lock name whose refcount to decrement.
        """
        with self._guard:
            entry = self._active.get(name)
            if entry is None:
                return  # Already cleaned up (should not happen in normal use)

            lock, count = entry
            if count > 1:
                self._active[name] = (lock, count - 1)
            else:
                del self._active[name]
                if len(self._free) < self._max_size:
                    self._free.append(lock)

    def populate(self, n: typing.Optional[int] = None, /) -> None:
        """
        Pre-populate the free list with *n* lock instances.

        Call this during backend initialisation to avoid factory overhead
        on the first burst of requests.

        :param n: Number of locks to pre-create. Clamped to *max_size*.
            If `None`, it defaults to `max_size` set on initialization.
        """
        with self._guard:
            if not n:
                n = self._max_size
            else:
                n = min(n, self._max_size - len(self._free))
            for _ in range(n):
                self._free.append(self._factory())

    @property
    def active_count(self) -> int:
        """Number of names currently holding at least one reference."""
        return len(self._active)

    @property
    def free_count(self) -> int:
        """Number of idle locks available in the free list."""
        return len(self._free)


@typing.final
class _NamedLockHandle(typing.Generic[AsyncLockT]):
    """
    A thin proxy returned by `_NamedLockPool.get`.

    Satisfies the `AsyncLock` protocol. On release it decrements the
    pool's reference count for the name and returns the underlying lock
    to the free list when no other task holds a reference to it.

    Instances are **not** reentrant and must not be shared across tasks.
    """

    __slots__ = ("_pool", "_name", "_lock", "_acquired")

    def __init__(
        self,
        pool: _NamedLockPool[AsyncLockT],
        name: str,
        lock: AsyncLockT,
    ) -> None:
        self._pool = pool
        self._name = name
        self._lock = lock
        self._acquired = False

    def locked(self) -> bool:
        return self._acquired

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the underlying lock.

        :param blocking: If False, return immediately when the lock is held elsewhere.
        :param blocking_timeout: Maximum seconds to wait when *blocking* is True.
        :return: True if acquired, False otherwise.
        """
        acquired = await self._lock.acquire(
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )
        self._acquired = acquired
        return acquired

    async def release(self) -> None:
        """
        Release the underlying lock and return it to the pool when the
        reference count for the name drops to zero.
        """
        if not self._acquired:
            raise RuntimeError(
                f"Cannot release lock '{self._name}': not acquired by this handle."
            )
        await self._lock.release()
        self._acquired = False
        self._pool._decrement_ref(self._name)

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        await self.release()


class _AsyncLockContext(typing.Generic[AsyncLockT]):
    """
    Asynchronous context manager for locks with optional lock TTL and blocking behavior.

    Multi-threaded/process or distributed safety depends on the underlying `AsyncLock` implementation.

    Warning: Using `ttl` or `blocking_timeout` with reentrant locks may cause unexpected behavior
    if nested contexts share the same lock instance.
    """

    __slots__ = (
        "_lock",
        "_ttl",
        "_blocking",
        "_blocking_timeout",
        "_ttl_task",
        "_acquired",
        "_auto_released",
    )

    def __init__(
        self,
        lock: AsyncLockT,
        ttl: typing.Optional[float] = None,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> None:
        """
        Initialize the async lock context.

        :param lock: The async lock instance to manage.
        :param ttl: How long the lock should live in seconds before auto-release (default: None = no auto-release).
        :param blocking: Whether to block when acquiring the lock (default: True).
        :param blocking_timeout: Max time to wait when acquiring lock (default: None = wait forever).
        """
        self._lock = lock
        self._ttl = ttl
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._ttl_task: typing.Optional[asyncio.Task] = None
        self._acquired: bool = False
        self._auto_released: bool = False

    async def __aenter__(self) -> Self:
        # Try to acquire the lock
        acquired = await self._lock.acquire(
            blocking=self._blocking,
            blocking_timeout=self._blocking_timeout,
        )
        if not acquired:
            raise LockTimeoutError(
                f"Could not acquire {type(self._lock).__qualname__!r} lock"
            )

        self._acquired = acquired
        # Start auto-release timer if acquired and timeout is set
        if acquired and self._ttl is not None:
            self._ttl_task = asyncio.create_task(self._auto_release())
        return self

    async def _auto_release(self) -> None:
        """Automatically releases the lock after the timeout."""
        if self._ttl is None:
            return

        try:
            await asyncio.sleep(self._ttl)
            # Only release if we still think we have the lock
            if self._acquired:
                try:
                    await self._lock.release()
                    self._auto_released = True
                    self._acquired = False
                except RuntimeError:
                    # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                    # and it may cause deadlocks if logging uses the same backend

                    # Lock might have been released already or not owned by us
                    # This can happen with reentrant locks
                    sys.stderr.write(
                        "Failed to auto-release lock after timeout; it may have been released already."
                        " This can happen with reentrant locks, and can lead to unexpected behavior.\n",
                    )
                    sys.stderr.flush()
        except asyncio.CancelledError:
            # Auto-release task cancelled because lock was released manually
            pass

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        # Ensure the lock is released and auto-release task cleaned up.
        # Cancel and wait for auto-release task completion
        if self._ttl_task is not None:
            self._ttl_task.cancel()
            try:
                await self._ttl_task
            except asyncio.CancelledError:
                pass

        # Release the lock if we acquired it and haven't released it yet
        if self._acquired and not self._auto_released:
            try:
                await self._lock.release()
            except RuntimeError:
                # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                # and it may cause deadlocks if logging uses the same backend

                # Lock might have been released by timeout in a race or not owned by current task
                sys.stderr.write(
                    "Failed to release lock on context exit; it may have been released already."
                    " This can happen with reentrant locks, and can lead to unexpected behavior.\n",
                )
                sys.stderr.flush()
            finally:
                self._acquired = False


class _AsyncFairRLock:
    """
    A fair reentrant lock for async programming. Fair means that it respects the order of acquisition.

    Adapted from: https://github.com/Joshuaalbert/Fair_AsyncRLock/blob/81e0d89d64c0cbc81a91c2f45992c79471ecc3bb/fair_async_rlock/fair_async_rlock.py
    """

    __slots__ = ("_owner", "_count", "_owner_transfer", "_queue")

    def __init__(self) -> None:
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._count = 0
        self._owner_transfer = False
        self._queue: deque[asyncio.Future[None]] = deque()

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        return self._owner == (task or asyncio.current_task())

    def locked(self) -> bool:
        """determines if the lock is being currently held or not"""
        return self._owner is not None

    async def acquire(self) -> typing.Literal[True]:
        """Acquire the lock."""
        current_task = asyncio.current_task()

        # If the lock is reentrant, acquire it immediately
        if self.is_owner(task=current_task):
            self._count += 1
            return True

        # If the lock is free (and ownership not in midst of transfer), acquire it immediately
        if self._count == 0 and not self._owner_transfer:
            self._owner = current_task
            self._count = 1
            return True

        # Only create future if we actually need to wait
        fut = asyncio.get_running_loop().create_future()
        self._queue.append(fut)

        # Wait for the lock to be free, then acquire
        try:
            await fut
            self._owner_transfer = False
            self._owner = current_task
            self._count = 1
        except asyncio.CancelledError:
            try:
                # If in queue, then cancelled before release
                self._queue.remove(fut)
            except ValueError:  # Otherwise, release happened, we were next.
                self._owner_transfer = False
                self._owner = current_task
                self._count = 1
                self._current_task_release()
            raise
        return True

    def _current_task_release(self) -> None:
        self._count -= 1
        if self._count == 0:
            self._owner = None
            if self._queue:
                # Wake up the next task in the queue
                self._queue.popleft().set_result(None)
                # Setting this here prevents another task getting lock until owner transfer.
                self._owner_transfer = True

    def release(self) -> None:
        """Release the lock"""
        current_task = asyncio.current_task()

        if self._owner is None:
            raise RuntimeError(
                f"Cannot release un-acquired lock. {current_task!r} tried to release."
            )

        if not self.is_owner(task=current_task):
            raise RuntimeError(
                f"Cannot release foreign lock. {current_task!r} tried to unlock {self._owner!r}."
            )

        self._current_task_release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        self.release()


class _AsyncRLock:
    """
    Unfair reentrant asyncio lock.

    This lock is reentrant per `asyncio.Task` but not FIFO / not fair. It may lower locking overhead than fair lock
    """

    __slots__ = ("_lock", "_owner", "_count")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: typing.Optional[asyncio.Task] = None
        self._count: int = 0

    def is_owner(self, task: typing.Optional[asyncio.Task] = None) -> bool:
        return self._owner is (task or asyncio.current_task())

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self) -> bool:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Must be called from within a task")

        # Reentrant fast-path
        if self._owner is current:
            self._count += 1
            return True

        # Acquire underlying unfair lock
        await self._lock.acquire()

        # Become owner
        self._owner = current
        self._count = 1
        return True

    def release(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Must be called from within a task")

        if self._owner is not current:
            raise RuntimeError("Cannot release a lock not owned by current task")

        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        self.release()
