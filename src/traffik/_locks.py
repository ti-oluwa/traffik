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
        self._owner: typing.Optional[asyncio.Task] = None
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


class _AsyncLockGroup:
    """
    Re-entrant async lock group.

    Acquires and releases a collection of `AsyncLock` atomically.
    """

    __slots__ = ("_locks", "_owner", "_counter", "_meta_lock")

    def __init__(self, *locks: AsyncLock) -> None:
        if not locks:
            raise ValueError("At least one lock must be provided to the lock group.")

        # Sort locks by id() to ensure consistent ordering and prevent deadlocks
        self._locks = sorted(locks, key=id)
        self._owner: typing.Optional[asyncio.Task] = None
        self._counter = 0
        self._meta_lock = asyncio.Lock()

    def locked(self) -> bool:
        return self._counter > 0

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """Acquire all locks in the group atomically."""
        current_task = asyncio.current_task()
        # Check for reentrant case
        async with self._meta_lock:
            if self._owner is current_task:
                self._counter += 1
                return True

        # Try to acquire all locks
        acquired: typing.List[AsyncLock] = []
        try:
            for lock in self._locks:
                if await lock.acquire(
                    blocking=blocking, blocking_timeout=blocking_timeout
                ):
                    acquired.append(lock)
                else:
                    # Failed to acquire, rollback
                    for acquired_lock in reversed(acquired):
                        await acquired_lock.release()
                    return False

            # Successfully acquired all locks
            async with self._meta_lock:
                self._owner = current_task
                self._counter = 1
            return True

        except Exception:
            # Rollback on any exception
            for acquired_lock in reversed(acquired):
                await acquired_lock.release()
            raise

    async def release(self) -> None:
        current_task = asyncio.current_task()
        should_release = False

        async with self._meta_lock:
            if self._owner is not current_task:
                raise RuntimeError("Cannot release lock not owned by current task")

            self._counter -= 1
            if self._counter == 0:
                self._owner = None
                should_release = True

        # Only release the actual locks after counter hits 0
        if should_release:
            # Release in reverse order (though with sorted locks, order is consistent)
            for lock in reversed(self._locks):
                await lock.release()

    async def __aenter__(self) -> Self:
        acquired = await self.acquire()
        if not acquired:
            raise LockTimeoutError("Could not acquire lock group")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        await self.release()
