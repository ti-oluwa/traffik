"""Traffik utilities."""

import asyncio
import base64
import functools
import inspect
import typing

import msgpack  # type: ignore[import-untyped]
from starlette.requests import HTTPConnection
from typing_extensions import TypeGuard

from traffik.config import (
    get_lock_blocking,
    get_lock_blocking_timeout,
    get_lock_ttl,
    set_lock_blocking,
    set_lock_blocking_timeout,
    set_lock_ttl,
)
from traffik.types import AwaitableCallable, T

# For backwards compatibility
get_lock_blocking = get_lock_blocking
get_lock_blocking_timeout = get_lock_blocking_timeout
get_lock_ttl = get_lock_ttl
set_lock_blocking = set_lock_blocking
set_lock_blocking_timeout = set_lock_blocking_timeout
set_lock_ttl = set_lock_ttl


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
    Return the current event loop time in seconds.

    Preferable to `time.time()` for duration calculations in async code.
    """
    return asyncio.get_running_loop().time()
