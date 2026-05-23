"""Locking utilities"""

import asyncio
import sys
import threading
import typing
from collections import deque
from time import time_ns
from types import TracebackType

from typing_extensions import Self

from traffik.exceptions import LockTimeoutError
from traffik.types import AsyncLock


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


class _NoOpLock:
    """
    Lightweight no-op context manager.

    Avoids using `contextlib.nullcontext()` for synchronization
    semantics.
    """

    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> bool:
        return False


@typing.final
class _NamedLockPool(typing.Generic[AsyncLockT]):
    """
    Refcounted reusable named async lock pool.

    Each unique name maps to exactly one underlying lock instance while
    references to that name exist. Multiple callers requesting the same
    name therefore contend on the same underlying lock instance.

    When all references to a name are released, the lock is either:

    - returned to the idle free list for reuse, or
    - discarded if the free list is already full.

    The pool maintains:

    - an active mapping (`name -> lock`)
    - an idle reusable lock cache
    - allocation accounting for bounded growth

    **Capacity Model**

    `max_size` controls the maximum number of idle reusable locks retained.

    `headroom` controls the maximum total allocations allowed:

        max_capacity = max_size * headroom

    This allows temporary bursts above the reusable cache size while still
    preventing unbounded memory growth.

    **Thread Safety**

    Metadata operations can optionally be guarded with a thread lock by
    passing `threadsafe=True`.

    This only protects internal pool bookkeeping structures.
    It does not make the underlying async lock implementation itself thread-safe.
    """

    __slots__ = (
        "_factory",
        "_max_size",
        "_headroom",
        "_max_capacity",
        "_allocated",
        "_free",
        "_active",
        "_guard",
    )

    def __init__(
        self,
        factory: typing.Callable[[], AsyncLockT],
        max_size: int = 128,
        headroom: int = 4,
        threadsafe: bool = False,
    ) -> None:
        """
        Initialize the lock pool.

        :param factory: Zero-argument callable producing a fresh async lock instance.
        :param max_size: Maximum number of idle reusable locks retained in the free list.
            Must be at least 1.
        :param headroom: Burst multiplier controlling maximum total lock allocations.
            Total allocation limit is: `max_capacity = max_size * headroom`.
            Must be at least 1.
        :param threadsafe: If True, pool metadata operations are guarded with a `threading.Lock`.
            Defaults to False.
        """
        if max_size < 1:
            raise ValueError("`max_size` must be at least 1.")

        if headroom < 1:
            raise ValueError("`headroom` must be at least 1.")

        self._factory = factory
        self._max_size = max_size
        self._headroom = headroom
        self._max_capacity = max_size * headroom
        self._allocated = 0

        self._free: list[AsyncLockT] = []
        """Idle reusable lock instances."""
        self._active: dict[str, tuple[AsyncLockT, int]] = {}
        """Mapping of name to (lock, reference_count)"""

        self._guard = threading.Lock() if threadsafe else _NoOpLock()

    def get(self, name: str, /) -> "_NamedLockHandle[AsyncLockT]":
        """
        Retrieve a named lock handle.

        Multiple handles retrieved for the same name share the same
        underlying lock instance.

        The reference count for the name is incremented immediately.

        :param name: Logical lock name.
        :return: `_NamedLockHandle` instance.
        """
        lock = self._increment_ref_or_create(name)
        return _NamedLockHandle(pool=self, name=name, lock=lock)

    def _increment_ref_or_create(self, name: str, /) -> AsyncLockT:
        """
        Increment the reference count for an existing name or allocate
        a new underlying lock.

        :param name: Logical lock name.
        :return: Underlying async lock instance.
        :raises RuntimeError: If the pool allocation limit is exceeded.
        """
        with self._guard:
            entry = self._active.get(name)
            if entry is not None:
                lock, refcount = entry
                self._active[name] = (lock, refcount + 1)
                return lock

            if self._free:
                lock = self._free.pop()
            else:
                if self._allocated >= self._max_capacity:
                    raise RuntimeError("Named lock pool maximum capacity exceeded.")

                lock = self._factory()
                self._allocated += 1

            self._active[name] = (lock, 1)
            return lock

    def _decrement_ref(self, name: str, /) -> None:
        """
        Decrement the reference count for a lock name.

        When the reference count reaches zero:

        - the name is removed from the active mapping
        - the lock is recycled into the free list if space exists
        - otherwise the lock is discarded

        :param name: Logical lock name.
        """
        with self._guard:
            entry = self._active.get(name)
            if entry is None:
                return

            lock, refcount = entry
            if refcount > 1:
                self._active[name] = (lock, refcount - 1)
                return

            del self._active[name]

            if hasattr(lock, "locked") and lock.locked():
                raise RuntimeError(
                    f"Attempted to recycle locked lock for name '{name}'."
                )

            if len(self._free) < self._max_size:
                self._free.append(lock)
                return

            self._discard_lock(lock)

    def _discard_lock(self, lock: AsyncLockT, /) -> None:
        """
        Permanently discard a lock instance.

        If the lock exposes a `discard()` method it will be called.

        Allocation accounting is updated accordingly.

        :param lock: Lock instance to discard.
        """
        discard = getattr(lock, "discard", None)
        if callable(discard):
            discard()
        self._allocated -= 1

    def populate(self, n: typing.Optional[int] = None, /) -> None:
        """
        Preallocate reusable idle locks.

        Useful during startup to avoid allocation overhead during the first traffic burst.

        :param n: Number of locks to create. If None, fills the free list up to `max_size`.
        """
        with self._guard:
            if n is None:
                n = self._max_size - len(self._free)
            else:
                n = min(n, self._max_size - len(self._free))

            remaining_capacity = self._max_capacity - self._allocated
            n = min(n, remaining_capacity)
            for _ in range(n):
                self._free.append(self._factory())
                self._allocated += 1

    @property
    def max_size(self) -> int:
        """Maximum retained idle reusable locks."""
        return self._max_size

    @property
    def headroom(self) -> int:
        """Burst allocation multiplier."""
        return self._headroom

    @property
    def max_capacity(self) -> int:
        """Maximum total allocated locks."""
        return self._max_capacity

    @property
    def allocated_count(self) -> int:
        """
        Total currently allocated locks.

        Includes active locks and idle reusable locks
        """
        return self._allocated

    @property
    def active_name_count(self) -> int:
        """Number of currently active logical lock names."""
        return len(self._active)

    @property
    def active_reference_count(self) -> int:
        """Total active references across all names."""
        return sum(refcount for _, refcount in self._active.values())

    @property
    def free_count(self) -> int:
        """Number of reusable idle locks currently cached."""
        return len(self._free)


@typing.final
class _NamedLockHandle(typing.Generic[AsyncLockT]):
    """
    Single-use, non-reentrant managed handle returned by `_NamedLockPool.get`.

    The handle wraps a pooled underlying async lock and ensures pool
    reference accounting is updated correctly during release.

    Handles are single-owner objects and must not be shared across tasks.
    """

    __slots__ = (
        "_pool",
        "_name",
        "_lock",
        "_acquired",
        "_released",
    )

    def __init__(
        self,
        pool: _NamedLockPool[AsyncLockT],
        name: str,
        lock: AsyncLockT,
    ) -> None:
        """
        Initialize the lock handle.

        :param pool: Owning lock pool.
        :param name: Logical lock name.
        :param lock: Underlying pooled lock instance.
        """
        self._pool = pool
        self._name = name
        self._lock = lock
        self._acquired = False
        # Flag to track release. This is to ensure that we do not try to acquire
        # an already used (released) named lock handle, has we have no way of
        # tracking that reliably back in the pool. Handles are therefore one-time use only.
        self._released = False

    def locked(self) -> bool:
        """
        Return whether **this handle** currently owns the lock.

        This does not necessarily reflect the underlying lock state.
        """
        return self._acquired

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the underlying lock.

        :param blocking: Whether to wait for lock acquisition.
        :param blocking_timeout: Maximum time to wait when blocking.
        :return: True if acquired, otherwise False.
        :raises RuntimeError: If this handle was already released.
        """
        if self._released:
            raise RuntimeError(
                f"Lock handle for '{self._name}' has already been released. Lock handle cannot be reused."
            )

        if self._acquired:
            raise RuntimeError(f"Lock handle for '{self._name}' is already acquired.")

        acquired = await self._lock.acquire(
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )
        self._acquired = acquired
        return acquired

    async def release(self) -> None:
        """
        Release the underlying lock and update pool reference accounting.

        :raises RuntimeError: If the handle does not currently own the lock.
        """
        if not self._acquired:
            raise RuntimeError(
                f"Cannot release lock '{self._name}': handle does not own the lock."
            )

        try:
            await self._lock.release()
        finally:
            self._acquired = False
            self._released = True
            self._pool._decrement_ref(self._name)

    async def __aenter__(self) -> Self:
        """
        Acquire the lock and return the handle.
        """
        acquired = await self.acquire()
        if not acquired:
            raise RuntimeError(f"Failed to acquire lock '{self._name}'.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        """Release the lock during async context manager exit."""
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
        "_timer_handle",
        "_acquired",
        "_reenterd",
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
            Does not apply (at context level) if the lock context is re-entered. It's left to the
            underlying lock to handle re-entrant timeout.
        """
        self._lock = lock
        self._ttl = ttl
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._timer_handle: typing.Optional[asyncio.TimerHandle] = None
        self._acquired = False
        self._reenterd = False
        self._auto_released = False

    async def __aenter__(self) -> Self:
        acquired = await self._lock.acquire(
            blocking=self._blocking,
            blocking_timeout=self._blocking_timeout,
        )
        if not acquired:
            raise LockTimeoutError(
                f"Could not acquire {type(self._lock).__qualname__!r} lock"
            )

        # Check if the context was re-entered. Re-entered context should not schedule
        # auto-release, or atleast I dont see any reason why it should as of now
        reentered = self._acquired is True and acquired is True
        self._acquired = acquired
        self._reenterd = reentered

        # Schedule auto-release if acquired (non-reentrant) and timeout is set
        if not reentered and acquired and self._ttl is not None:
            self._timer_handle = asyncio.get_running_loop().call_later(
                self._ttl, self._auto_release
            )
        return self

    async def _auto_release(self) -> None:
        """Automatic lock release callback."""
        if self._ttl is None:
            return

        # Only release if we still think we have the lock
        if self._acquired:
            try:
                # We cant shield release here. It's very unsafe
                await self._lock.release()
                self._auto_released = True
                self._acquired = False
            except RuntimeError as exc:
                # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                # and it may cause deadlocks if logging uses the same backend

                # Lock might have been released already or not owned by us
                # This can happen with reentrant locks
                sys.stderr.write(
                    f"Failed to release lock after timeout; it may have been released already.\n"
                    f"This can happen with reentrant locks, and can lead to unexpected behavior.\n {exc}",
                )
                sys.stderr.flush()

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        # Ensure the lock is released and auto-release timer cleaned up.
        if not self._reenterd and self._timer_handle is not None:
            self._timer_handle.cancel()

        # Release the lock if we acquired it and haven't released it yet
        if self._acquired and not self._auto_released:
            try:
                # Shield release we need release to complete even if parent task is cancelled
                # If not, there's a higher chance of having stale/deadlocks
                await asyncio.shield(self._lock.release())
            except RuntimeError as exc:
                # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                # and it may cause deadlocks if logging uses the same backend

                # Lock might have been released by timeout in a race or not owned by current task
                sys.stderr.write(
                    f"Failed to release lock on context exit; it may have been released already."
                    f" This can happen with reentrant locks, and can lead to unexpected behavior.\n {exc}",
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
