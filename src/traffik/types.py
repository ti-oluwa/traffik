"""Type definitions and protocols for the traffik package."""

from dataclasses import dataclass
import typing

from starlette.requests import HTTPConnection
from starlette.responses import Response
from typing_extensions import ParamSpec, TypeAlias, TypedDict

from traffik.rates import Rate

__all__ = [
    "UNLIMITED",
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

UNLIMITED = object()
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

ErrorHandler = typing.Callable[
    [HTTPConnectionT, Exception, int], typing.Awaitable[WaitPeriod]
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
