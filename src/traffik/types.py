"""Type definitions and protocols."""

import enum

import math
import typing
from collections.abc import Mapping
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
    [HTTPConnectionT, Exception], typing.Union[Response, typing.Awaitable[Response]]
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
    """TypedDict defining configuration for locks."""

    ttl: typing.Optional[float]
    """Maximum time to wait for the lock to be released in seconds."""
    blocking: bool
    """Whether to block when acquiring the lock."""
    blocking_timeout: typing.Optional[float]
    """Maximum time to wait for the lock in seconds."""


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
    [HTTPConnectionTcon, WaitPeriod, T, typing.Dict[str, typing.Any]],
    typing.Awaitable[typing.Any],
]
"""
Type definition for connection throttled handlers.

Takes the connection, wait period, the throttle, and context as parameters.
Returns an awaitable response.
"""

RateFunc = typing.Callable[
    [HTTPConnectionT, typing.Optional[typing.Dict[str, typing.Any]]],
    typing.Awaitable[Rate],
]
"""Type definition for a rate function."""
CostFunc = typing.Callable[
    [HTTPConnectionT, typing.Optional[typing.Dict[str, typing.Any]]],
    typing.Awaitable[int],
]
"""Type definition for a cost function."""

RateType = typing.Union[Rate, RateFunc[HTTPConnectionT], str]
"""Type definition for rate specifications."""
CostType = typing.Union[int, CostFunc[HTTPConnectionT]]
"""Type definition for cost specifications."""


BackoffStrategy = typing.Callable[[int, float], float]
"""
A callable that implements a backoff strategy.

Takes the current attempt number and base delay, and returns the delay in seconds before the next retry.
"""


class _TransactionExceptionInfo(TypedDict):
    """Information about an exception that occurred during throttling transaction."""

    connection: HTTPConnection
    exception: BaseException
    attempt: int
    cost: int
    context: typing.Optional[typing.Mapping[str, typing.Any]]


RetryOn = typing.Union[
    typing.Type[BaseException],
    typing.Tuple[typing.Type[BaseException], ...],
    typing.Callable[
        [_TransactionExceptionInfo],
        typing.Union[bool, typing.Awaitable[bool]],
    ],
]
"""
Type definition for retry decision logic.

Can be either:
- A single exception type to retry on
- A tuple of exception types to retry on
- A callable that takes (connection, exception, cost, context, attempt) and returns bool
"""

ApplyOnError = typing.Union[
    bool, typing.Type[BaseException], typing.Tuple[typing.Type[BaseException], ...]
]
"""
Type definition for applying throttles on error.

Can be either:
- `False`: Don't apply on any exception
- `True`: Apply on all exceptions
- A single exception type to apply on
- A tuple of exception types to apply on
"""


class Dependency(typing.Protocol, typing.Generic[P, Rco]):
    """Protocol for dependencies that can be used in FastAPI routes."""

    def __call__(
        self,
        *args: P.args,
        **kwargs: P.kwargs,  # Although FastAPI passes arguments as keyword arguments to dependencies
    ) -> typing.Union[Rco, typing.Awaitable[Rco]]: ...


MapT = typing.TypeVar("MapT", bound=Mapping)


@dataclass(frozen=True)
class StrategyStat(typing.Generic[MapT]):
    """Statistics for a throttling strategy."""

    key: Stringable
    """The throttling key."""
    rate: Rate
    """The rate limit definition."""
    hits_remaining: float
    """Number of hits remaining in the current period."""
    wait_ms: WaitPeriod
    """Time to wait (in milliseconds) before the next allowed request."""
    metadata: typing.Optional[MapT] = None
    """Additional metadata related to the strategy."""


@typing.runtime_checkable
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
