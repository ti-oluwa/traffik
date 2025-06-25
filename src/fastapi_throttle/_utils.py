import typing
import inspect
import ipaddress
from starlette.requests import HTTPConnection
import fastapi.params

from fastapi_throttle._typing import P, R, Q, S


def get_ip_address(
    connection: HTTPConnection,
) -> typing.Optional[typing.Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """
    Returns the IP address of the connection client.

    :param connection: The HTTP connection
    """
    x_forwarded_for = connection.headers.get(
        "x-forwarded-for"
    ) or connection.headers.get("remote-addr")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0]
    else:
        ip = connection.client.host if connection.client else None
    if not ip:
        return None
    return ipaddress.ip_address(ip)


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


class DecoratorDepends(
    typing.Generic[P, R, Q, S],
    fastapi.params.Depends,
):
    """
    `fastapi.params.Depends` subclass that allows instances to be used as decorators.

    Instances use `dependency_decorator` to apply the dependency to the decorated object,
    while still allowing usage as regular FastAPI dependencies.

    `dependency_decorator` is a callable that takes the decorated object and an optional dependency
    and returns the decorated object with/without the dependency applied.

    Think of the `dependency_decorator` as a chef that mixes the sauce (dependency)
    with the dish (decorated object), making a dish with the sauce or without it.
    """

    def __init__(
        self,
        dependency_decorator: typing.Callable[
            [
                typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
                typing.Optional[
                    typing.Callable[Q, typing.Union[S, typing.Awaitable[S]]]
                ],
            ],
            typing.Callable[P, typing.Union[R, typing.Awaitable[R]]],
        ],
        dependency: typing.Optional[
            typing.Callable[Q, typing.Union[S, typing.Awaitable[S]]]
        ] = None,
        *,
        use_cache: bool = True,
    ) -> None:
        self.dependency_decorator = dependency_decorator
        super().__init__(dependency, use_cache=use_cache)

    def __call__(
        self, decorated: typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]
    ) -> typing.Callable[P, typing.Union[R, typing.Awaitable[R]]]:
        return self.dependency_decorator(decorated, self.dependency)
