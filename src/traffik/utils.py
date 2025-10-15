import asyncio
import functools
import inspect
import ipaddress
import logging
import typing

from starlette.requests import HTTPConnection
from typing_extensions import Self, TypeGuard

from traffik.exceptions import LockTimeoutError
from traffik.types import AsyncLock, AwaitableCallable, T

logger = logging.getLogger(__name__)


def get_ip_address(
    connection: HTTPConnection,
) -> typing.Optional[typing.Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """
    Returns the IP address of the connection client.

    This function attempts to extract the IP address from the `x-forwarded-for` header
    or the `remote-addr` header. If neither is present, it falls back to the `client.host`
    attribute of the connection.

    :param connection: The HTTP connection
    :return: The IP address of the connection client, or None if it cannot be determined.
    """
    x_forwarded_for = connection.headers.get(
        "x-forwarded-for"
    ) or connection.headers.get("remote-addr")
    if x_forwarded_for:
        return ipaddress.ip_address(x_forwarded_for.split(",")[0].strip())

    if connection.client:
        return ipaddress.ip_address(connection.client.host)
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


def time() -> float:
    """Return the current event loop time."""
    return asyncio.get_event_loop().time()


AsyncLockT = typing.TypeVar("AsyncLockT", bound=AsyncLock)


class AsyncLockContext(typing.Generic[AsyncLockT]):
    """
    Asynchronous context manager for locks with optional timeout auto-release.

    Thread-safety depends on the underlying `AsyncLock` implementation.

    Warning: Using `release_timeout` with reentrant locks may cause unexpected behavior
    if nested contexts share the same lock instance.
    """

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
                f"Could not acquire {type(self._lock).__name__!r} lock"
            )

        self._acquired = acquired
        # Start auto-release timer if acquired and timeout is set
        if acquired and self._release_timeout is not None:
            self._timer = asyncio.create_task(self._release_on_timeout())
        return self

    async def _release_on_timeout(self) -> None:
        """Automatically release the lock after the timeout."""
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
                    # Lock might have been released already or not owned by us
                    # This can happen with reentrant locks
                    logger.warning(
                        "Failed to auto-release lock after timeout; it may have been released already."
                        "This can happen with reentrant locks, and can lead to unexpected behavior.",
                    )
                    pass
        except asyncio.CancelledError:
            # Timer cancelled because lock was released manually
            pass

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Ensure the lock is released and timer cleaned up."""
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
                # Lock might have been released by timeout in a race
                # or not owned by current task
                logger.warning(
                    "Failed to release lock on context exit; it may have been released already."
                    "This can happen with reentrant locks, and can lead to unexpected behavior.",
                )
                pass
            finally:
                self._acquired = False


class AsyncRLock:
    """
    A reentrant async lock for use in asyncio contexts.

    Keeps same API as `asyncio.Lock`
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._counter = 0

    def locked(self):
        return self._counter > 0

    async def acquire(self):
        current_task = asyncio.current_task()
        # Reentrant case - already own the lock
        if self._owner == current_task:
            self._counter += 1
            return True

        # Must acquire the underlying lock
        await self._lock.acquire()
        self._owner = current_task
        self._counter = 1
        return True

    def release(self):
        current_task = asyncio.current_task()
        if self._owner != current_task:
            raise RuntimeError("Lock can only be released by the owner task.")

        self._counter -= 1
        if self._counter == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.release()


class AsyncLockGroup:
    """
    Reentrant async group lock.

    Acquires and releases a collection of `AsyncLock` instances atomically.
    """

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
            if self._owner == current_task:
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
            if self._owner != current_task:
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

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.release()
