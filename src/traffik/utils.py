"""Traffik utilities."""

import asyncio
import base64
import functools
import inspect
import sys
import time as pytime
import typing
from types import TracebackType

import msgpack  # type: ignore[import-untyped]
from starlette.requests import HTTPConnection
from typing_extensions import Self, TypeGuard

from traffik.config import (  # noqa
    get_lock_blocking,
    get_lock_blocking_timeout,
    get_lock_ttl,
    set_lock_blocking,
    set_lock_blocking_timeout,
    set_lock_ttl,
)
from traffik.types import AwaitableCallable, T


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


def _add_parameter_to_signature(
    func: typing.Callable, parameter: inspect.Parameter, index: int = -1
) -> typing.Callable:
    """
    Adds a parameter to the function's signature at the specified index.

    Useful when you need to modify the signature of a function
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
    # False

    # This will fail because the signature of the wrapper function is overridden by the original function's signature,
    # when functools.wraps is used. To fix this, we can use the `_add_parameter_to_signature` function.

    wrapped_func = _add_parameter_to_signature(
        func=wrapped_func,
        parameter=inspect.Parameter(
            name="new_param",
            kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str
        ),
        index=0 # Add the new parameter at the beginning
    )
    assert "new_param" in inspect.signature(wrapped_func).parameters
    # True

    # This way any new parameters added to the wrapper function will be preserved and logic using the
    # function's signature will respect the new parameters.
    ```
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


# Borrowed from Starlette's `is_async_callable` utility
@typing.overload
def is_async_callable(obj: AwaitableCallable[T]) -> TypeGuard[AwaitableCallable[T]]: ...


@typing.overload
def is_async_callable(obj: typing.Any) -> TypeGuard[AwaitableCallable[typing.Any]]: ...


def is_async_callable(obj: typing.Any) -> typing.Any:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(obj.__call__)  # type: ignore
    )


def dump_data(obj: typing.Any) -> str:
    """
    Serialize an object to msgpack bytes, then base64 encode as string.

    Uses msgpack for fast, compact binary serialization.
    Approximately 5-10x faster and 30-50% smaller than JSON.

    :param obj: Object to serialize (dict, list, int, float, str, bytes, etc.)
    :return: Base64-encoded msgpack string
    """
    packed: bytes = msgpack.packb(obj, use_bin_type=True)  # type: ignore[no-untyped-call]
    # Encode to base85 for slightly better compression than base64
    return base64.b85encode(packed).decode("ascii")


def load_data(data: str) -> typing.Any:
    """
    Deserialize base64-encoded msgpack string to a Python object.

    :param data: Base64-encoded msgpack string to deserialize
    :return: Deserialized Python object
    :raises msgpack.exceptions.UnpackException: If data is corrupted
    """
    packed = base64.b85decode(data.encode("ascii"))
    return msgpack.unpackb(packed, raw=False)  # type: ignore[no-any-return, no-untyped-call]


MsgPackDecodeError = msgpack.exceptions.UnpackException
"""Exception raised for data decoding errors from msgpack."""


def time() -> float:
    """
    Return the current UTC time as a Unix timestamp in seconds.

    Uses wall-clock time (`time.time()`) to ensure window calculations
    remain consistent across process restarts and multiple servers/processes.
    """
    return pytime.time()


class TaskTimer:
    """
    Timer and asynchronous context manager that cancels the current task if the
    block of code after or inside it (depending on usage context), takes longer than a specified timeout.

    Adapted from `emcache.timeout.OpTimeout` in the `emcache` library (MIT License).
    Copyright (c) 2020-2024 Pau Freixes
    """

    __slots__ = (
        "_done",
        "_error",
        "_loop",
        "_task",
        "_timed_out",
        "_timeout",
        "_timer_handler",
    )

    def __init__(
        self,
        timeout: typing.Optional[float],
        loop: asyncio.AbstractEventLoop,
        error: typing.Optional[BaseException] = None,
    ):
        """
        Initialize the timer.

        :param timeout: The timeout duration in seconds. If None, no timeout is applied.
        :param loop: The asyncio event loop to use for scheduling the timeout.
        :param error: Optional custom exception to raise on timeout.
            If None, defaults to `asyncio.TimeoutError`.
        """
        self._timed_out = False
        self._timeout = timeout
        self._loop = loop
        self._error = (
            asyncio.TimeoutError("Operation timed out") if error is None else error
        )
        # Will be set to the current task when the timer is started
        self._task: typing.Optional[asyncio.Task[typing.Any]] = None
        self._timer_handler: typing.Optional[asyncio.TimerHandle] = None
        self._done = False

    def timed_out(self) -> bool:
        """Indicates whether the timeout was triggered."""
        return self._timed_out

    def done(self) -> bool:
        """Indicates whether the timeout has been stopped or triggered."""
        return self._done

    def cancelled(self) -> bool:
        """Indicates whether the timeout was cancelled (i.e. stopped without being triggered)."""
        return self._done and not self._timed_out

    def _on_timeout(self) -> None:
        if self._task is not None and not self._task.done():
            self._timed_out = True
            self._task.cancel()

    def start(self) -> None:
        """
        Start the timer.

        Calling this once will start the timer. If the timer is already running, this method does nothing.
        """
        if self._done or self._timed_out:
            raise RuntimeError(
                f"Cannot start {self.__class__.__name__}: already cancelled or timed out."
            )
        if self._timeout is not None and self._timer_handler is None:
            task = asyncio.current_task(loop=self._loop)
            if not task:
                raise RuntimeError(
                    f"{self.__class__.__name__} must be used within an active asyncio task"
                )
            self._task = task
            self._timer_handler = self._loop.call_later(self._timeout, self._on_timeout)

    def _handle_timed_out(self, exc_type: typing.Type[BaseException]) -> None:
        """
        Handle the case where the timeout was triggered.

        If the timeout was triggered and the current exception matches the specified type (or any type if None),
        it will be treated as a timeout cancellation. This method will raise the timeout error and suppress
        the context of the cancellation.

        :param exc_type: The type of the current exception being handled, or None if not currently handling an exception.
        :raises: The timeout error if the timeout was triggered and the exception type matches.
        """
        if sys.version_info[:2] >= (3, 11) and self._task is not None:
            # Call uncancel to clear cancellation state from TaskTimer
            self._task.uncancel()
        if exc_type is asyncio.CancelledError:
            # it's not a real cancellation, was a timeout
            raise self._error from None  # suppress context of cancellation

    def stop(
        self, exc_type: typing.Optional[typing.Type[BaseException]] = None
    ) -> None:
        """
        Stop (and cancel) the timer, handling any timeout cancellation and propagation
        if the timer was triggered.

        Once this method is called, the timer is considered done and cannot be restarted.

        :param exc_type: Optional exception type to check for cancellation.
            If the timeout was triggered and the current exception matches this type,
            it will be treated as a timeout cancellation. Defaults to None, which means
            any exception will be treated as a timeout cancellation if the timeout was triggered.
        :raises: The timeout error if the timeout was triggered and the exception type matches.
        """
        if self._done:
            raise RuntimeError(
                f"Cannot stop {self.__class__.__name__}: already cancelled or timed out."
            )

        self._done = True
        if self._timed_out and exc_type is not None:
            self._handle_timed_out(exc_type)

        # Cancel the timer if it's still active
        if self._timer_handler is not None and not self._timer_handler.cancelled():
            self._timer_handler.cancel()
            self._timer_handler = None

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[TracebackType],
    ) -> bool:
        if not self._done:
            self.stop(exc_type)
        return False
