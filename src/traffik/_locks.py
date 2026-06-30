"""Locking utilities"""

import asyncio
import itertools
import os
import secrets
import typing
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from types import TracebackType

from typing_extensions import Self

from traffik.exceptions import (
    LockAcquisitionError,
    LockPoolError,
    LockReleaseError,
    LockTimeoutError,
)
from traffik.typing import AsyncLock
from traffik.utils import TaskTimer

__PREFIX = secrets.token_hex(8)
__PID = f"{os.getpid():08x}"
__COUNTER = itertools.count()


def get_token() -> str:
    """Return a safe/unique (fence) token."""
    return f"{__PREFIX}{next(__COUNTER):016x}{__PID}"


AsyncLockT = typing.TypeVar("AsyncLockT", bound=AsyncLock)


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

    **Capacity Model:**

    `max_size` controls the maximum number of idle reusable locks retained.

    `headroom` controls the maximum total allocations allowed:

        max_capacity = max_size * headroom

    This allows temporary bursts above the reusable cache size while still
    preventing unbounded memory growth.

    **Note:** This is not thread-safe and is designed to be used within a single `asyncio` event loop.
        If you need thread safety, consider holding an external lock around the pool ops.
    """

    __slots__ = (
        "_allocated",
        "_closed",
        "_factory",
        "_headroom",
        "_idle",
        "_in_use",
        "_max_capacity",
        "_max_size",
    )

    def __init__(
        self,
        factory: typing.Callable[[], AsyncLockT],
        max_size: int = 128,
        headroom: int = 4,
    ) -> None:
        """
        Initialize the lock pool.

        :param factory: Zero-argument callable producing a fresh async lock instance.
        :param max_size: Maximum number of idle reusable locks retained in the free list.
            Must be at least 1.
        :param headroom: Burst multiplier controlling maximum total lock allocations.
            Total allocation limit is: `max_capacity = max_size * headroom`.
            Must be at least 1.
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

        self._idle: typing.List[AsyncLockT] = []
        """Idle reusable lock instances."""
        self._in_use: typing.Dict[str, typing.Tuple[AsyncLockT, int]] = {}
        """Mapping of in-use names to (lock, reference_count)"""
        self._closed = False

    def get(self, name: str, /) -> "_NamedLockHandle[AsyncLockT]":
        """
        Retrieve a named lock handle.

        Multiple handles retrieved for the same name share the same
        underlying lock instance.

        The reference count for the name is incremented immediately.

        :param name: Logical lock name for the handle. This is used for reference counting
            and lock reuse, but is not exposed to the underlying lock instance.
        :return: `_NamedLockHandle` instance.
        :raises RuntimeError: If the pool allocation limit is exceeded.
        """
        if self._closed:
            raise LockPoolError("Cannot get lock handle from closed pool.")

        entry = self._in_use.get(name)
        if entry is not None:
            lock, refcount = entry
            self._in_use[name] = (lock, refcount + 1)
            return _NamedLockHandle(pool=self, name=name, lock=lock)

        if self._idle:
            lock = self._idle.pop()
        else:
            if self._allocated >= self._max_capacity:
                raise LockPoolError("Named lock pool maximum capacity exceeded.")

            lock = self._factory()
            self._allocated += 1

        self._in_use[name] = (lock, 1)
        return _NamedLockHandle(pool=self, name=name, lock=lock)

    @asynccontextmanager
    async def lock(
        self, name: str
    ) -> typing.AsyncGenerator["_NamedLockHandle[AsyncLockT]", None]:
        """
        Async context manager for getting and releasing a named lock handle.

        This convenience context manager ensure that the lock handle is
        always released and/or discarded on exit. This does not acquire the handle on entry.
        You'll have to call `handle.acquire()` in the context or just get the handle
        and use its context manager directly.

        :param name: Logical lock name.
        :return: `_NamedLockHandle` instance.
        """
        handle = self.get(name)
        try:
            yield handle
        finally:
            if handle._acquired:
                await handle.release()
            else:
                handle.discard()

    def _release(self, name: str, /) -> None:
        """
        Release a named lock handle, updating reference counts and
        recycling or discarding the underlying lock as needed.

        When the reference count reaches zero:

        - the name is removed from the active mapping
        - the lock is recycled into the free list if space exists
        - otherwise the lock is discarded

        :param name: Logical lock name for the handle being released.
        """
        if self._closed:
            return

        entry = self._in_use.get(name)
        if entry is None:
            return

        lock, refcount = entry
        if refcount > 1:
            self._in_use[name] = (lock, refcount - 1)
            return

        del self._in_use[name]

        if len(self._idle) < self._max_size:
            self._idle.append(lock)
            return

        # Discard the lock if it can't be recycled
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
        if self._closed:
            raise LockPoolError("Cannot populate locks in closed registry.")

        if n is None:
            n = self._max_size - len(self._idle)
        else:
            n = min(n, self._max_size - len(self._idle))

        remaining_capacity = self._max_capacity - self._allocated
        n = min(n, remaining_capacity)
        for _ in range(n):
            self._idle.append(self._factory())
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
        return len(self._in_use)

    @property
    def active_reference_count(self) -> int:
        """Total active references across all names."""
        return sum(refcount for _, refcount in self._in_use.values())

    @property
    def free_count(self) -> int:
        """Number of reusable idle locks currently cached."""
        return len(self._idle)

    @property
    def size(self) -> int:
        """Return the current size of the lock pool. Both in-use and free"""
        return len(self._in_use) + len(self._idle)

    @property
    def closed(self) -> bool:
        """Whether the pool is closed."""
        return self._closed

    def close(self) -> None:
        """
        Close the pool and release all resources.

        Idempotent and safe to call multiple times.

        After closing, the pool will reject new requests and all existing handles are effectively invalidated.
        """
        if self._closed:
            return

        self._closed = True
        for lock, _ in self._in_use.values():
            self._discard_lock(lock)
        self._in_use.clear()

        for lock in self._idle:
            self._discard_lock(lock)
        self._idle.clear()


@typing.final
class _NamedLockHandle(typing.Generic[AsyncLockT]):
    """
    Single-use, non-reentrant managed handle returned by `_NamedLockPool.get`.

    The handle wraps a pooled underlying async lock and ensures pool
    reference accounting is updated correctly during release.

    Although the underlying lock may be reentrant, this handle is not.
    To utilize reentrancy, get another handle to the same lock like so:

    ```python
    from contextlib import closing

    pool = _NamedLockPool(factory=lambda: MyLock())
    with closing(pool): # Auto-closes pool
        # Get an handle for 'key'
        async with pool.get('key'):
            # Protected code runs...

            async with pool.get("key'): # Reenter the underlying lock by getting another handle
                # Nested code runs...
            # Reentry release

        # Final release
    # Pool closed
    ```

    **Handles are single-task objects and must not be shared across tasks.**
    """

    __slots__ = (
        "__weakref__",
        "_acquired",
        "_lock",
        "_name",
        "_pool",
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

        :param pool: The `_NamedLockPool` instance that created this handle.
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

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        """Return True if the specified task (or current task if None) owns the lock."""
        return self._lock.is_owner(task=task)

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the underlying lock.

        :param blocking: Whether to wait for lock acquisition.
        :param blocking_timeout: Maximum time to wait when blocking. `None` means wait forever.
            Does not apply if `blocking` is False.
        :return: True if acquired, otherwise False.
        :raises LockAcquisitionError: If this handle was already released.
        """
        if self._released:
            raise LockAcquisitionError(
                f"Lock handle for '{self._name}' has already been released. Lock handle cannot be reused."
            )

        if self._acquired:
            raise LockAcquisitionError(
                f"Lock handle for '{self._name}' is already acquired."
            )

        acquired = await self._lock.acquire(
            blocking=blocking,
            blocking_timeout=blocking_timeout,
        )
        self._acquired = acquired
        return acquired

    async def release(self) -> None:
        """
        Release the underlying lock and update pool reference accounting.

        :raises LockReleaseError: If the handle does not currently own the lock.
        """
        if not self._acquired:
            raise LockReleaseError(
                f"Cannot release lock '{self._name}': handle does not own the lock."
            )

        try:
            await self._lock.release()
        finally:
            self._acquired = False
            self._released = True
            self._pool._release(self._name)

    def discard(self) -> None:
        """
        Discard this (unused) lock handle without releasing the underlying lock.
        This should only be used when the lock has not been acquired.

        This is useful for cleaning up handles that were retrieved but never acquired,
        to ensure pool reference counts are updated correctly and locks are not leaked.
        """
        if not self._acquired and not self._released:
            self._released = True
            self._pool._release(self._name)

    async def __aenter__(self) -> Self:
        """
        Acquire the lock and return the handle.
        """
        if not await self.acquire():
            raise LockAcquisitionError(f"Failed to acquire lock '{self._name}'.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        """Release the lock during async context manager exit."""
        await self.release()


class _AsyncFairRLock:
    """
    A fair `asyncio.Task` reentrant lock for async programming.

    Fair means that it respects the order of acquisition.

    Adapted from: https://github.com/Joshuaalbert/Fair_AsyncRLock/blob/81e0d89d64c0cbc81a91c2f45992c79471ecc3bb/fair_async_rlock/fair_async_rlock.py
    """

    __slots__ = ("_count", "_owner", "_owner_transfer", "_queue")

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
        """
        Acquire the lock.

        Never call with `asyncio.wait_for` as that will
        break task ownership tracking between `acquire`
        and `release` especially in Python3.11+.

        `asyncio.wait_for` wraps `acquire in a new `asyncio.Task`.
        So calling `asyncio.current_task()` in `acquire` give the new /wrapper task,
        and give the caller (intended) task in `release`, cause a mismatch
        when ownership is check later on
        """
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
                f"Cannot release foreign lock. {current_task!r} tried to unlock lock owned by {self._owner!r}."
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

    __slots__ = ("_count", "_lock", "_owner")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: typing.Optional[asyncio.Task[typing.Any]] = None
        self._count: int = 0

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        return self._owner is (task or asyncio.current_task())

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self) -> bool:
        """
        Acquire the lock.

        Never call with `asyncio.wait_for` as that will
        break task ownership tracking between `acquire`
        and `release` especially in Python3.11+.

        `asyncio.wait_for` wraps `acquire in a new `asyncio.Task`.
        So calling `asyncio.current_task()` in `acquire` give the new /wrapper task,
        and give the caller (intended) task in `release`, cause a mismatch
        when ownership is check later on.
        """
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
        """Release the lock"""
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


@typing.final
class _AsyncLockContext(typing.Generic[AsyncLockT]):
    """
    Async context manager for `AsyncLock` with optional TTL and blocking controls.

    This wrapper is (intentionally) non-reentrant per instance. Each `backend.lock(name)`
    call creates a fresh context instance. The underlying lock (e.g. `_AsyncFairRLock`)
    may itself be reentrant, but this wrapper only tracks one acquire/release cycle and
    raises a `LockAcquisitionError` if entered twice.

    **TTL semantics:**

    When `ttl` is set, the body is cancelled if execution exceeds the
    timeout. The lock is always released before the error propagates, ensuring the
    distributed lock is never left dangling. `ttl` also caps the acquire wait when
    `blocking_timeout` is not set explicitly.

    Each context instance must be used by exactly one `asyncio.Task`. The underlying
    lock implementation is responsible for cross-task safety.

    **Exception priority:**

    When multiple errors coincide the priority is:
    - `LockTimeoutError` (TTL fired) always surfaces; lock is released first.
    - Body exception only surfaces when TTL did not fire.
    - `LockReleaseError` surfaces only when no other exception is already propagating.
    """

    __slots__ = (
        "_acquired",
        "_blocking",
        "_blocking_timeout",
        "_lock",
        "_timer",
        "_ttl",
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

        :param lock: The async lock to manage.
        :param ttl: Maximum seconds the body may run after the lock is
            acquired. When the deadline fires the running task is cancelled
            and `LockTimeoutError` is raised. Also used as the acquire-wait
            upper bound when `blocking_timeout` is not set.

            **Warning:** If the lock provided uses a release TTL, you may want to pass it as `ttl` here too.
            This is crucial for distributed `AsyncLock`s because if not set, the lock may have been
            released (by TTL) on the distributed server (e.g redis server) but the client/task still thinks it's holding
            the lock and continues executing the body until it tries to release, which may cause unsafe execution
            without the mutual exclusion. To ensure that the TTL is is also enforced locally too, and also
            propagated to the server if needed, pass the same TTL value here, but ideally the local TTL should
            be slightly more conservative (smaller) than the server TTL to account for clock skew and network latency.

        :param blocking: If `False`, fail immediately when the lock is busy.
        :param blocking_timeout: Maximum seconds to wait during acquire.
            Takes priority over `ttl` for the acquire-wait bound.
        :param
        """
        self._lock = lock
        self._ttl = ttl
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._acquired = False
        self._timer: typing.Optional[TaskTimer] = None

    async def _acquire(self) -> None:
        """
        Acquire the underlying lock, respecting the effective acquire timeout.

        :raises LockTimeoutError: Timed out waiting for the lock.
        :raises LockAcquisitionError: Non-blocking acquire returned False immediately.
        """
        acquired = await self._lock.acquire(
            blocking=self._blocking,
            blocking_timeout=self._blocking_timeout,
        )
        if not acquired:
            raise LockAcquisitionError(
                f"Could not acquire {type(self._lock).__qualname__!r} lock (non-blocking)."
            )

    async def _release(self, exc_type: typing.Optional[type[BaseException]]) -> None:
        """
        Release the underlying lock.

        If release fails and no exception is already propagating, raises
        `LockReleaseError`. If an exception is already in flight the
        release error is suppressed so the original exception wins.
        """
        try:
            await self._lock.release()
        except (RuntimeError, TimeoutError) as release_exc:
            if exc_type is None:
                raise LockReleaseError(
                    f"Failed to release {type(self._lock).__qualname__!r} lock."
                ) from release_exc
        finally:
            self._acquired = False

    async def __aenter__(self) -> Self:
        if self._acquired:
            raise LockAcquisitionError(
                f"Lock context for {type(self._lock).__qualname__!r} is already "
                f"acquired. {self.__class__.__name__} is not re-entrant; create a new "
                f"context instance for nested locking."
            )

        await self._acquire()
        self._acquired = True

        # Start the lock hold-time watchdog after the lock is confirmed acquired.
        # `TaskTimer` schedules task.cancel() via `call_later`. The `CancelledError`
        # that bubbles up through the body is then converted to `LockTimeoutError`
        # inside `__aexit__` before the lock is released.
        if self._ttl is not None:
            loop = asyncio.get_running_loop()
            self._timer = TaskTimer(
                timeout=self._ttl,
                loop=loop,
                error=LockTimeoutError(
                    f"{type(self._lock).__qualname__!r} lock TTL of {self._ttl}s "
                    "expired while the lock was held. The body was cancelled to "
                    "prevent unsafe execution without mutual exclusion."
                ),
            )
            self._timer.start()

        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        # First and most important, we need to exit `TaskTimer`
        # This must happen before the lock release so that:
        # - A normal exit cancels the `call_later` handle (hence no spurious fire).
        # - A TTL-fired exit converts `CancelledError` to `LockTimeoutError`.
        # We then stash any `LockTimeoutError` and re-raise it after releasing.
        timeout_exc: typing.Optional[BaseException] = None
        if self._timer is not None:
            timer = self._timer
            self._timer = None
            try:
                timer.stop(exc_type)
            except LockTimeoutError as ltexc:
                # TTL watchdog fired. Stash and release first.
                timeout_exc = ltexc
            except BaseException as bexc:
                # We got and unexpected error from `TaskTimer` itself. Release then propagate.
                if self._acquired:
                    await self._release(exc_type=type(bexc))
                raise

        # Now we can release the lock (if it was acquired) and then surface any
        # TTL error that may have fired during the body execution or the release.
        if self._acquired:
            # If the TTL fired, the active exception is `LockTimeoutError`,
            # not whatever exc_type was when `__aexit__` was called.
            # Pass the real active exception type so `_release` knows not to raise on top of it.
            active_exc_type = type(timeout_exc) if timeout_exc is not None else exc_type
            await self._release(exc_type=active_exc_type)

        # Since the lock should have been released by now, we can now raise any `LockTimeoutError`.
        if timeout_exc is not None:
            raise timeout_exc


@typing.final
class _NamedGateRegistry:
    """
    Process-local registry of per-name asyncio gates with contention-aware
    lazy creation and refcounting.

    Gates are only created when local contention is detected. Specifically,
    when the number of tasks currently trying to acquire the same lock name
    reaches `contention_threshold`. Tasks below the threshold bypass the gate
    entirely and go straight to the underlying (distributed) lock, paying zero
    gate overhead.

    This design means:
    - Low load / diverse keys: nearly every acquire bypasses the gate.
    - High load, same key: gates activate automatically when contention
      reaches the threshold, serializing local tasks so only one races
      on the network at a time.

    **Waiter counting vs refcounting:**

    `_waiters[name]` tracks how many tasks are currently inside an acquire
    call for a given name (from `get` until the finally block in
    `_GatedNamedLock.acquire`). This is distinct from "holding the lock" -
    a task that won the distributed lock has already exited its acquire call
    and decremented the waiter count. The gate only exists during the race
    to acquire, not during the hold.

    **Gate lifecycle:**

    Created lazily when `_waiters[name]` first reaches `contention_threshold`.
    Destroyed when `_waiters[name]` drops back to zero (all racing tasks
    have finished their acquire attempts).

    `get` returns a `_NamedGateHandle` when a gate is active, or `None` when
    the caller is below the contention threshold. Every `get` call (whether
    it returns None or a handle) increments `_waiters[name]` and must be
    paired with exactly one decrement, which happens either via
    `_NamedGateHandle.release` or via `_release` directly in the None path.
    The `gate` context manager handles this automatically.

    **Note:** This is not thread-safe and is designed to be used within a single `asyncio` event loop.
        If you need thread safety, consider holding an external lock around the registry ops.
    """

    __slots__ = (
        "_closed",
        "_contention_threshold",
        "_gates",
        "_waiters",
    )

    def __init__(self, contention_threshold: int = 1) -> None:
        """
        Initialize the gate registry.

        :param contention_threshold: Number of concurrent waiters required
            before a gate is created for a name. Defaults to 1, meaning a
            gate is created as soon as a second task tries to acquire the
            same name (i.e. when `_waiters[name]` reaches 1 on a get call
            that finds an existing waiter count of >= contention_threshold).

            Increasing this allows more tasks to race on the network before
            local serialization kicks in. Useful when the distributed lock
            is cheap relative to the gate overhead, or when false contention
            (tasks acquiring different logical resources that hash to the same
            name) would cause unnecessary serialization.

            Must be >= 1. Setting it to 1 (default) means any second waiter
            triggers a gate. Setting it to N means up to N tasks race freely
            before gating begins.
        """
        if contention_threshold < 1:
            raise ValueError("`contention_threshold` must be at least 1.")
        self._contention_threshold = contention_threshold
        self._gates: typing.Dict[str, asyncio.Lock] = {}
        self._waiters: typing.Dict[str, int] = {}
        self._closed = False

    def get(self, name: str) -> typing.Optional["_NamedGateHandle"]:
        """
        Register a new waiter for the given name and return a gate handle
        if local contention has reached the threshold, or None otherwise.

        The waiter count for this name is incremented unconditionally.
        A gate handle is returned only when the pre-increment count is
        >= `contention_threshold`, meaning this task is not the first
        (or first N) to arrive and should queue locally rather than
        racing on the network immediately.

        Every call to `get` must result in exactly one decrement of the
        waiter count, either via `_NamedGateHandle.release()` on the
        returned handle, or via `_release(name)` directly when None is
        returned. The `gate` context manager handles this automatically
        and is the preferred way to use this method.

        :param name: Logical lock name to gate on.
        :return: A `_NamedGateHandle` if this task should wait for the
            gate before proceeding, or None if it should go straight to
            the underlying lock.
        """
        if self._closed:
            # The gate registry is essentially a gate pool, hence the use of the `LockPoolError` here
            raise LockPoolError("Cannot get gate handle from closed registry.")

        count = self._waiters.get(name, 0)
        self._waiters[name] = count + 1

        if count < self._contention_threshold:
            # Below threshold. This task races on the network directly.
            return None

        # At or above threshold. Gate this task locally.
        if name not in self._gates:
            self._gates[name] = asyncio.Lock()
        return _NamedGateHandle(registry=self, name=name, lock=self._gates[name])

    @contextmanager
    def gate(self, name: str) -> typing.Iterator[typing.Optional["_NamedGateHandle"]]:
        """
        Synchronous context manager that registers a waiter, yields a gate
        handle (or None), and guarantees the waiter count is decremented on exit.

        This is the preferred way to use the registry. It ensures the waiter
        count is always decremented regardless of exceptions or early returns,
        and releases the gate handle if one was created.

        **Note:** this is a synchronous context manager because the registry operations themselves
        are synchronous. The yielded `_NamedGateHandle.acquire` is still async and must be
        awaited by the caller.

        Usage looks like this:

        ```python
        with self._registry.gate(name) as gate:
            if gate is None:
                return await self._lock.acquire(...)

            # Acquire local gate first before proceeding to
            # attempt distibuted lock acquisition
            if not await gate.acquire(timeout=...):
                return False
            return await self._lock.acquire(...)
        ```

        :param name: Logical lock name to gate on.
        :yields: A `_NamedGateHandle` if contention threshold is reached,
            or None if the caller should bypass the gate.
        """
        handle = self.get(name)
        try:
            yield handle
        finally:
            if handle is not None and handle._acquired:
                handle.release()
            else:
                # Release the waiter count for the None path directly,
                # since no handle was created to do it.
                self._release(name)

    def _release(self, name: str) -> None:
        """
        Decrement the waiter count for the given name.

        Destroys the gate when the count reaches zero, freeing the
        lock for GC. Called by `_NamedGateHandle.release` and
        `_NamedGateHandle.discard` for handle-based releases, and
        directly by the `gate` context manager for the None path.

        :param name: Logical gate name being released.
        """
        if self._closed:
            # If the registry is closed, we can skip the release since all gates are
            # already cleared and no new ones can be created.
            return

        count = self._waiters.get(name, 0) - 1
        if count <= 0:
            self._waiters.pop(name, None)
            self._gates.pop(name, None)
        else:
            self._waiters[name] = count

    def set_contention_threshold(self, threshold: int) -> None:
        """
        Update threshold at runtime.

        Takes effect on next get() call.
        Existing waiters already past the old threshold
        continue normally - no disruption to in-flight acquires.
        """
        if threshold < 1:
            raise ValueError("`contention_threshold` must be at least 1.")
        self._contention_threshold = threshold

    @property
    def size(self) -> int:
        """Return the current size of the registry. Number of active gates"""
        return len(self._gates)

    @property
    def contention_threshold(self) -> int:
        """Waiter count at which gates begin to be created for a name."""
        return self._contention_threshold

    @property
    def active_gate_count(self) -> int:
        """Number of names currently tracked. Useful for testing."""
        return len(self._gates)

    @property
    def closed(self) -> bool:
        """Whether the registry is closed."""
        return self._closed

    def close(self) -> None:
        """
        Close all gates and clear the registry.

        Idempotent and thread-safe. After this call, all existing gates are cleared
        and any future calls to `get` will raise `RuntimeError`.

        Note: Once closed, the registry cannot be used again. This is intended for cleanup.
        """
        if self._closed:
            return

        self._closed = True
        self._gates.clear()
        self._waiters.clear()


@typing.final
class _NamedGateHandle:
    """
    Single-use managed handle returned by `_NamedGateRegistry.get`.

    Wraps a shared `asyncio.Lock` for one acquire/release lifecycle,
    managing both the asyncio.Lock state and the registry waiter count.

    **Single-use contract:**

    Each handle represents one task's slot in the local serialization queue.
    Once released (or discarded), the handle cannot be reused. The registry
    creates a fresh handle for each `get` call.

    **What the gate does and does not do:**

    The gate serializes local tasks that are racing to acquire a distributed
    lock. It does not represent ownership of the distributed lock itself.
    The gate is held only during the acquire race and released immediately
    after, whether the underlying lock was acquired or not. This means:

    - A task that wins the distributed lock does not hold the gate while
      it executes its critical section. Other local tasks may race on the
      distributed lock during this time (the distributed lock handles mutual exclusion).

    - The gate only prevents N local tasks from all hitting the network
      simultaneously. It does not prevent a new task from going to the
      network while another holds the distributed lock.

    **Weakref finalization:**

    A weakref finalizer calls `discard` if this handle is GC'd without an
    explicit `release` call, preventing waiter count leaks. Since `discard`
    may call `asyncio.Lock.release()` from a GC context (outside the event
    loop), it is sync. This is safe only because a GC'd handle implies no
    task holds a reference to it, but see the note in `discard` for the
    caveat about waiters on the shared lock.
    """

    __slots__ = (
        "__weakref__",
        "_acquired",
        "_lock",
        "_name",
        "_registry",
        "_released",
    )

    def __init__(
        self,
        registry: _NamedGateRegistry,
        name: str,
        lock: asyncio.Lock,
    ) -> None:
        """
        :param registry: Registry that created this handle. Used to
            decrement the waiter count on release.
        :param name: Logical gate name. Used for waiter count management.
        :param lock: The shared asyncio.Lock for this gate name. Shared
            across all handles with the same name that are active
            simultaneously.
        """
        self._registry = registry
        self._name = name
        self._lock = lock
        self._acquired = False
        self._released = False

    def discard(self) -> None:
        """
        Release this handle without raising, intended for GC finalization.

        If the gate was acquired, it releases the `asyncio.Lock` (which may
        wake a waiting local task). Always decrements the registry waiter
        count.

        **Caveat:** calling `asyncio.Lock.release()` from a GC finalizer
        runs outside the event loop. This is technically unsafe if there
        are other tasks waiting on this lock, since waking them requires
        scheduling on the correct event loop. In practice, a GC'd handle
        means the task that created it has no reference to it, which
        typically means it completed or was cancelled. If the task was
        cancelled after acquiring the gate but before releasing it, this
        finalizer fires and correctly unblocks the next waiter - but the
        scheduling may be on the wrong thread. This edge case only occurs
        on improper usage (not using the context manager). Always use the
        async context manager or call `release()` explicitly.
        """
        if not self._released:
            if self._acquired:
                try:
                    self._lock.release()
                except RuntimeError:
                    pass
            self._released = True
            self._registry._release(self._name)

    async def acquire(self, timeout: typing.Optional[float] = None) -> bool:
        """
        Attempt to acquire the gate.

        Uses `asyncio.wait_for` for all finite timeouts including zero,
        since `asyncio.Lock` has no `acquire_nowait()`. A timeout of 0
        yields control to the event loop once; if the lock is not
        immediately available it raises `TimeoutError` and returns False.

        :param timeout: Maximum seconds to wait.
            None: wait forever (no wait_for wrapper, lower overhead).
            0: non-blocking; return False immediately if gate is held.
            >0: wait up to this many seconds.
        :return: True if the gate was acquired, False if the timeout
            expired before the gate became available.
        :raises LockAcquisitionError: If this handle has already been
            released or is already in the acquired state.
        """
        if self._released:
            raise LockAcquisitionError(
                f"Gate handle for '{self._name}' has already been released "
                "and cannot be reused."
            )
        if self._acquired:
            raise LockAcquisitionError(
                f"Gate handle for '{self._name}' is already acquired."
            )

        if timeout is None:
            await self._lock.acquire()
            self._acquired = True
            return True

        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=timeout)
            self._acquired = True
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        """
        Release the gate and decrement the registry waiter count.

        Releases the `asyncio.Lock` if it was acquired (unblocking the next
        local task queued behind this gate) and decrements the waiter count
        in the registry (allowing the gate to be destroyed when no more
        tasks are racing for this name).

        Idempotent. Subsequent calls after the first are no-ops.
        :raises LockReleaseError: If this handle was never acquired or has already
        """
        if self._released:
            return

        if not self._acquired:
            raise LockReleaseError(
                f"Cannot release gate handle for '{self._name}' that was never acquired."
            )

        try:
            self._lock.release()
        finally:
            self._released = True
            self._acquired = False
            self._registry._release(self._name)

    async def __aenter__(self) -> Self:
        """Acquire the gate, raising on failure."""
        if not await self.acquire():
            raise LockAcquisitionError(f"Could not acquire gate '{self._name}'.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        """Release the gate."""
        if self._acquired:
            self.release()
        else:
            # Even if we failed to acquire the gate, we still need to release
            # our slot in the registry's waiter count.
            self._registry._release(self._name)


@typing.final
class _GatedNamedLock(typing.Generic[AsyncLockT]):
    """
    Named distributed `AsyncLock` proxy that adds process-local task
    serialization via `_NamedGateRegistry`.

    The idea behind the usage of this is to prevent a thundering-herd problem
    on a distributed server (with spinning lock) when there is high/very high contention
    on a distributed lock key.

    **Warning:** For low-contention scenarios, this just staggers distributed lock acquisition
    tries and will likely add more overhead and reduce throughput and/or increase
    latency. The registry used should therefore use a reasonable `contention_threshold` based on
    expected load and contention patterns (on the backend).

    **When the gate is active (contention at or above threshold):**

    Local tasks queue behind a per-name `asyncio.Lock` gate before racing
    on the distributed lock. Only one task per process makes a network
    call at a time, reducing network traffic from `O(N x spins)` to `O(N)`.
    Where `N` is the number of tasks.

    **When the gate is inactive (below contention threshold):**

    Tasks bypass the gate entirely and go straight to the underlying
    distributed lock. (Almost) Zero gate overhead.

    **Semantics for `blocking_timeout`:**

    When `blocking_timeout` is set and a gate is active, the timeout is
    treated as a single deadline spanning both the gate wait and the
    underlying lock acquire. This preserves the caller's expectation that
    the total wait will not exceed `blocking_timeout` seconds:

        blocking_timeout = 5.0
        Gate wait:            3.0s  (deadline - loop.time() passed to gate)
        Remaining for lock:   2.0s  (deadline - loop.time() after gate)
        Total:                5.0s  Correct - the caller's deadline is respected.

    Not:
        Gate wait:            3.0s
        Lock wait:            5.0s  (full timeout passed again)
        Total:                8.0s  Wrong - the caller expected to wait at most 5 seconds total.
    """

    __slots__ = ("_lock", "_name", "_registry")

    def __init__(
        self,
        lock: AsyncLockT,
        name: str,
        registry: _NamedGateRegistry,
    ) -> None:
        """
        Initialize the gated named lock.

        :param lock: The underlying non-reentrant (spinning / distributed) `AsyncLock` to proxy.
        :param name: Logical lock name. Used for gate registry lookups.
        :param registry: The per-backend gate registry instance.
        """
        self._lock = lock
        self._name = name
        self._registry = registry

    def is_owner(self, task: typing.Optional[asyncio.Task[typing.Any]] = None) -> bool:
        """Check if the current task (or provided task) owns the underlying lock."""
        return self._lock.is_owner(task=task)

    async def acquire(
        self,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> bool:
        """
        Acquire the gate (if contention warrants it), then acquire the
        underlying distributed lock.

        **Gate behavior:**

        If the registry returns None (below contention threshold), the
        underlying lock is acquired directly with the original blocking
        semantics unchanged.

        If the registry returns a gate handle (contention at or above
        threshold), the gate is acquired first with a timeout derived
        from the remaining deadline, then the underlying lock is acquired
        with whatever time remains.

        **Non-blocking behavior (`blocking=False`):**

        Both the gate and the underlying lock must be immediately
        acquirable. If the gate is held (another local task is racing),
        returns False immediately without attempting the network call.
        If the gate is free but the distributed lock is held, returns
        False after one network attempt.

        **Blocking with timeout (`blocking=True, blocking_timeout=N`):**

        The timeout is a single deadline. Time spent waiting for the gate
        reduces the time available for the underlying lock acquire. If the
        deadline expires at any point - before gate acquisition, between
        gate and lock, or during lock acquire - returns False.

        **Blocking without timeout (`blocking=True, blocking_timeout=None`):**

        Waits indefinitely for both gate and underlying lock.

        :param blocking: If False, return immediately if either the gate
            or the underlying lock is unavailable.
        :param blocking_timeout: Single deadline in seconds covering the
            entire acquire attempt including any gate wait. None means
            wait forever. Ignored when blocking=False.
        :return: True if the distributed lock was acquired, False otherwise.
        """
        # If the current task already owns the underlying lock, we can skip the gate
        # and delegate the ability to reenter the underlying lock to the underlying lock.
        # That is, if the lock is reentrant, the task can reenter it as many times as it wants
        # without going through the gate again. Else, the underlying lock will most likely
        # raise on the reentrant acquire attempt.
        if self._lock.is_owner():
            return await self._lock.acquire(
                blocking=blocking,
                blocking_timeout=blocking_timeout,
            )

        with self._registry.gate(self._name) as gate:
            if gate is None:
                # Below contention threshold. No gate required.
                return await self._lock.acquire(
                    blocking=blocking,
                    blocking_timeout=blocking_timeout,
                )

            if not blocking:
                if not await gate.acquire(timeout=0):
                    return False
                return await self._lock.acquire(blocking=False, blocking_timeout=None)

            if blocking_timeout is None:
                if not await gate.acquire(timeout=None):
                    return False
                return await self._lock.acquire(
                    blocking=True,
                    blocking_timeout=None,
                )

            # Single deadline spanning gate wait and lock acquire.
            loop = asyncio.get_running_loop()
            now = loop.time()
            deadline = now + blocking_timeout
            gate_timeout = deadline - now
            if gate_timeout <= 0:
                return False

            if not await gate.acquire(timeout=gate_timeout):
                return False

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False

            return await self._lock.acquire(
                blocking=True,
                blocking_timeout=remaining,
            )

    async def release(self) -> None:
        """Release the underlying distributed lock."""
        await self._lock.release()

    async def __aenter__(self) -> Self:
        if not await self.acquire():
            raise LockAcquisitionError(f"Could not acquire gated lock '{self._name}'.")
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        await self.release()
