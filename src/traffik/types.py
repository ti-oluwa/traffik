import sys
import typing

from starlette.requests import HTTPConnection
from typing_extensions import ParamSpec, TypeAlias, Unpack

P = ParamSpec("P")
Q = ParamSpec("Q")
R = typing.TypeVar("R")
S = typing.TypeVar("S")
T = typing.TypeVar("T")


UNLIMITED = object()
"""
A sentinel value to identify that a connection should not be throttled.

This value should be returned by the connection identifier function
when the connection should not be subject to throttling.
"""


class Stringable(typing.Protocol):
    """Protocol for objects that can be converted to a string."""

    def __str__(self) -> str:
        """Return a string representation of the object."""
        ...


Function: TypeAlias = typing.Callable[P, R]
CoroutineFunction: TypeAlias = typing.Callable[P, typing.Awaitable[R]]
Decorated: TypeAlias = typing.Union[Function[P, R], CoroutineFunction[P, R]]
Dependency: TypeAlias = typing.Union[Function[Q, S], CoroutineFunction[Q, S]]

HTTPConnectionT = typing.TypeVar("HTTPConnectionT", bound=HTTPConnection)
ConnectionIdentifier: TypeAlias = typing.Callable[
    [HTTPConnectionT], typing.Awaitable[typing.Union[Stringable, typing.Any]]
]

WaitPeriod: TypeAlias = int
if sys.version_info >= (3, 12):
    _Args = typing.Tuple[typing.Any, ...]
    ConnectionThrottledHandler: TypeAlias = typing.Callable[
        [HTTPConnectionT, WaitPeriod, Unpack[_Args]], typing.Awaitable[typing.Any]
    ]
else:
    ConnectionThrottledHandler: TypeAlias = typing.Callable[
        [HTTPConnectionT, WaitPeriod], typing.Awaitable[typing.Any]
    ]
