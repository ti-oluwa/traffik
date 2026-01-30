"""`traffik` utilities."""

import asyncio # noqa: I001
from collections import deque
import functools
import inspect
import threading
from time import time_ns
from types import TracebackType
import typing

from starlette.requests import HTTPConnection
from typing_extensions import Self, TypeGuard

from traffik.exceptions import LockTimeoutError
from traffik.types import AsyncLock, AwaitableCallable, T

try:
    import orjson as json  # type: ignore[import]
except ImportError:
    import json  # type: ignore[no-redef]


def get_remote_address(connection: HTTPConnection) -> typing.Optional[str]:
    """
    Returns the Remote/IP address of the connection client.

    This function attempts to extract the IP address from the `x-forwarded-for` header
    or the `remote-addr` header. If neither is present, it falls back to the `client.host`
    attribute of the connection.

    :param connection: The HTTP connection
    :return: The Remote/IP address of the connection client, or None if it cannot be determined.
    """
    x_forwarded_for = connection.headers.get(
        "x-forwarded-for"
    ) or connection.headers.get("remote-addr")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    if connection.client:
        return connection.client.host
    return None


def add_parameter_to_signature(
    func: typing.Callable, parameter: inspect.Parameter, index: int = -1
) -> typing.Callable:
    """
    Adds a parameter to the function's signature at the specified index.

    This may be useful when you need to modify the signature of a function
    dynamically, such as when adding a new parameter to a function (via a decorator/wrapper).

    :param func: The function to update.
    :param parameter: The parameter to add.
    :param index: The index at which to add the parameter. Negative indices are supported.
        Default is -1, which adds the parameter at the end.
        If the index greater than the number of parameters, a ValueError is raised.
    :return: The updated function.

    Example Usage:
    ```python
    import inspect
    import typing
    import functools
    from typing_extensions import ParamSpec

    P = ParamSpec("P")
    R = TypeVar("R")


    def my_func(a: int, b: str = "default"):
        pass

    def decorator(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
        def _wrapper(new_param: str, *args: P.args, **kwargs: P.kwargs) -> R:
            return func(*args, **kwargs)

        return functools.wraps(func)(_wrapper)

    wrapped_func = decorator(my_func) # returns wrapper function
    assert "new_param" in inspect.signature(wrapped_func).parameters
    >>> False

    # This will fail because the signature of the wrapper function is overridden by the original function's signature,
    # when functools.wraps is used. To fix this, we can use the `add_parameter_to_signature` function.

    wrapped_func = add_parameter_to_signature(
        func=wrapped_func,
        parameter=inspect.Parameter(
            name="new_param",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str
        ),
        index=0 # Add the new parameter at the beginning
    )
    assert "new_param" in inspect.signature(wrapped_func).parameters
    >>> True

    # This way any new parameters added to the wrapper function will be preserved and logic using the
    # function's signature will respect the new parameters.
    """
    sig = inspect.signature(func)
    params = list(sig.parameters.values())

    # Check if the index is valid
    if index < 0:
        index = len(params) + index + 1
    elif index > len(params):
        raise ValueError(
            f"Index {index} is out of bounds for the function's signature. Maximum index is {len(params)}. "
            "Use a '-1' index to add the parameter at the end, if needed."
        )

    params.insert(index, parameter)
    new_sig = sig.replace(parameters=params)
    func.__signature__ = new_sig  # type: ignore
    return func


# Borrowed from Starlette's is_async_callable utility
@typing.overload
def is_async_callable(obj: AwaitableCallable[T]) -> TypeGuard[AwaitableCallable[T]]: ...


@typing.overload
def is_async_callable(obj: typing.Any) -> TypeGuard[AwaitableCallable[typing.Any]]: ...


def is_async_callable(obj: typing.Any) -> typing.Any:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return asyncio.iscoroutinefunction(obj) or (
        callable(obj) and asyncio.iscoroutinefunction(obj.__call__)  # type: ignore
    )


def dump_json(data: typing.Any) -> str:
    """
    Serialize data to a JSON string using `orjson` if available, otherwise falls back to the built-in `json` module.

    :param data: The data to serialize.
    :return: The serialized JSON string.
    """
    dumped_data = json.dumps(data)
    if isinstance(dumped_data, bytes):
        return dumped_data.decode("utf-8")
    return dumped_data


def load_json(data: str) -> typing.Any:
    """
    Deserialize a JSON string to a Python object using `orjson` if available, otherwise falls back to the built-in `json` module.

    :param data: The JSON string to deserialize.
    :return: The deserialized Python object.
    """
    return json.loads(data)


JSONDecodeError = json.JSONDecodeError  # type: ignore[attr-defined]
"""Exception raised for JSON decoding errors. Either from `orjson` or built-in `json` module."""


def time() -> float:
    """
    Return the current event loop time in seconds.

    Preferable to `time.time()` for duration calculations in async code.
    """
    return asyncio.get_event_loop().time()


class FenceTokenGenerator:
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


fence_token_generator = FenceTokenGenerator()
"""Global fence token generator instance."""


AsyncLockT = typing.TypeVar("AsyncLockT", bound=AsyncLock)


class AsyncLockContext(typing.Generic[AsyncLockT]):
    """
    Asynchronous context manager for locks with optional timeout auto-release.

    Muilti-threaded or Distributed safety depends on the underlying `AsyncLock` implementation.

    Warning: Using `release_timeout` or `blocking_timeout` with reentrant locks may cause unexpected behavior
    if nested contexts share the same lock instance.
    """

    __slots__ = (
        "_lock",
        "_release_timeout",
        "_blocking",
        "_blocking_timeout",
        "_timer",
        "_acquired",
        "_released_by_timeout",
    )

    def __init__(
        self,
        lock: AsyncLockT,
        release_timeout: typing.Optional[float] = None,
        blocking: bool = True,
        blocking_timeout: typing.Optional[float] = None,
    ) -> None:
        self._lock = lock
        self._release_timeout = release_timeout
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._timer: typing.Optional[asyncio.Task] = None
        self._acquired: bool = False
        self._released_by_timeout: bool = False

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
        if acquired and self._release_timeout is not None:
            self._timer = asyncio.create_task(self._release_on_timeout())
        return self

    async def _release_on_timeout(self) -> None:
        """Automatically releases the lock after the timeout."""
        if self._release_timeout is None:
            return

        try:
            await asyncio.sleep(self._release_timeout)
            # Only release if we still think we have the lock
            if self._acquired:
                try:
                    await self._lock.release()
                    self._released_by_timeout = True
                    self._acquired = False
                except RuntimeError:
                    # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                    # and it may cause deadlocks if logging uses the same backend

                    # Lock might have been released already or not owned by us
                    # This can happen with reentrant locks
                    print(
                        "Failed to auto-release lock after timeout; it may have been released already."
                        " This can happen with reentrant locks, and can lead to unexpected behavior.",
                    )
                    pass
        except asyncio.CancelledError:
            # Timer cancelled because lock was released manually
            pass

    async def __aexit__(
        self,
        exc_type: typing.Optional[type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> None:
        # Ensure the lock is released and timer cleaned up.
        # Cancel and wait for timer completion
        if self._timer is not None:
            self._timer.cancel()
            try:
                await self._timer
            except asyncio.CancelledError:
                pass

        # Release the lock if we acquired it and haven't released it yet
        if self._acquired and not self._released_by_timeout:
            try:
                await self._lock.release()
            except RuntimeError:
                # This needs to be a fast operation. Cannot use logger here as it blocks the event loop,
                # and it may cause deadlocks if logging uses the same backend

                # Lock might have been released by timeout in a race or not owned by current task
                print(
                    "Failed to release lock on context exit; it may have been released already."
                    " This can happen with reentrant locks, and can lead to unexpected behavior.",
                )
                pass
            finally:
                self._acquired = False


class AsyncRLock:
    """
    A fair reentrant lock for async programming. Fair means that it respects the order of acquisition.

    Adapted from: https://github.com/Joshuaalbert/FairAsyncRLock/blob/81e0d89d64c0cbc81a91c2f45992c79471ecc3bb/fair_async_rlock/fair_async_rlock.py
    """

    __slots__ = ("_owner", "_count", "_owner_transfer", "_queue", "_loop")

    def __init__(self) -> None:
        self._owner: typing.Optional[asyncio.Task] = None
        self._count = 0
        self._owner_transfer = False
        self._queue: deque[asyncio.Future[None]] = deque()
        self._loop: typing.Optional[asyncio.AbstractEventLoop] = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if not self._loop:
            self._loop = asyncio.get_event_loop()
        return self._loop

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
        fut = self.loop.create_future()
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


class AsyncLockGroup:
    """
    Reentrant async group lock.

    Acquires and releases a collection of `AsyncLock` instances atomically.
    """

    __slots__ = ("_locks", "_owner", "_counter", "_meta_lock")

    def __init__(self, locks: typing.Sequence[AsyncLock]) -> None:
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
                    # Failed to acquire - rollback
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

        async with self._meta_lock:
            if self._owner is not current_task:
                raise RuntimeError("Cannot release lock not owned by current task")

            self._counter -= 1
            if self._counter == 0:
                self._owner = None
                # Release metadata lock before releasing actual locks
                # to avoid holding it during potentially slow operations

        # Only release the actual locks after counter hits 0
        if self._counter == 0:
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
