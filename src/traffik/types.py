"""Type definitions and protocols."""

import typing
from dataclasses import dataclass

from starlette.requests import HTTPConnection
from starlette.responses import Response
from typing_extensions import ParamSpec, TypeAlias, TypedDict

from traffik.rates import Rate

__all__ = [
    "EXEMPTED",
    "HTTPConnectionT",
    "HTTPConnectionTcon",
    "WaitPeriod",
    "Matchable",
    "ExceptionHandler",
    "LockConfig",
    "Stringable",
    "ConnectionIdentifier",
    "ConnectionThrottledHandler",
    "Dependency",
    "AsyncLock",
]

P = ParamSpec("P")
Q = ParamSpec("Q")
R = typing.TypeVar("R")
S = typing.TypeVar("S")
T = typing.TypeVar("T")
Rco = typing.TypeVar("Rco", covariant=True)

EXEMPTED = object()
"""
A sentinel value to identify that a connection should not be throttled.

This value should be returned by the connection identifier function
when the connection should not be subject to throttling.
"""


Function: TypeAlias = typing.Callable[P, R]
CoroutineFunction: TypeAlias = typing.Callable[P, typing.Awaitable[R]]

HTTPConnectionT = typing.TypeVar("HTTPConnectionT", bound=HTTPConnection)
HTTPConnectionTcon = typing.TypeVar(
    "HTTPConnectionTcon", bound=HTTPConnection, contravariant=True
)
WaitPeriod: TypeAlias = float

Matchable: TypeAlias = typing.Union[str, typing.Pattern[str]]
"""A type alias for a matchable path, which can be a string or a compiled regex pattern."""
ExceptionHandler: TypeAlias = typing.Callable[
    [HTTPConnection, Exception], typing.Union[Response, typing.Awaitable[Response]]
]

AwaitableCallable = typing.Callable[..., typing.Awaitable[T]]

MappingT = typing.TypeVar("MappingT", bound=typing.Mapping[typing.Any, typing.Any])
ThrottleErrorHandler = typing.Callable[
    [HTTPConnectionT, MappingT], typing.Awaitable[WaitPeriod]
]
"""
A callable that handles errors during throttling.

Takes the connection, the exception, and the cost as parameters.
Returns an integer representing the wait period in milliseconds.
"""


class LockConfig(TypedDict, total=False):
    """TypedDict for lock configuration parameters."""

    blocking: bool
    """Whether to block when acquiring the lock."""
    blocking_timeout: typing.Optional[float]
    """Maximum time to wait for the lock in seconds."""
    release_timeout: typing.Optional[float]
    """Maximum time to wait for the lock to be released in seconds."""


class Stringable(typing.Protocol):
    """Protocol for objects that can be converted to a string."""

    def __str__(self) -> str:
        """Return a string representation of the object."""
        ...


ConnectionIdentifier = typing.Callable[
    [HTTPConnectionTcon], typing.Awaitable[typing.Union[Stringable, typing.Any]]
]
"""Type definition for connection identifier functions."""

ConnectionThrottledHandler = typing.Callable[
    [HTTPConnectionTcon, WaitPeriod, typing.Dict[str, typing.Any]],
    typing.Awaitable[typing.Any],
]
"""Type definition for connection throttled handlers."""

RateFunc = typing.Callable[
    [HTTPConnectionT, typing.Mapping[str, typing.Any]], typing.Awaitable[Rate]
]
"""Type definition for rate functions."""
CostFunc = typing.Callable[
    [HTTPConnectionT, typing.Mapping[str, typing.Any]], typing.Awaitable[int]
]
"""Type definition for cost functions."""

RateType = typing.Union[Rate, RateFunc[HTTPConnectionT], str]
"""Type definition for rate specifications."""
CostType = typing.Union[int, CostFunc[HTTPConnectionT]]
"""Type definition for cost specifications."""


class Dependency(typing.Protocol, typing.Generic[P, Rco]):
    """Protocol for dependencies that can be used in FastAPI routes."""

    def __call__(
        self,
        *args: P.args,
        **kwargs: P.kwargs,  # Although FastAPI passes arguments as keyword arguments to dependencies
    ) -> typing.Union[Rco, typing.Awaitable[Rco]]: ...


@dataclass(frozen=True)
class StrategyStat:
    """Statistics for a throttling strategy."""

    key: Stringable
    rate: Rate
    hits_remaining: float
    wait_time: WaitPeriod


class AsyncLock(typing.Protocol):
    """Protocol for asynchronous lock objects."""

    def locked(self) -> bool:
        """Check if the lock is currently held."""
        ...

    async def acquire(
        self, blocking: bool, blocking_timeout: typing.Optional[float]
    ) -> bool:
        """
        Acquire the lock.

        :param blocking: If False, return immediately if the lock is held.
        :param blocking_timeout: Max time (seconds) to wait if blocking is True.
        :return: True if the lock was acquired, False otherwise.
        """
        ...

    async def release(self) -> None:
        """Release the lock."""
        ...
