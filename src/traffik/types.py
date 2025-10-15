import typing
import types
from starlette.requests import HTTPConnection
from starlette.responses import Response
from typing_extensions import ParamSpec, TypeAlias

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
WaitPeriod: TypeAlias = int

Matchable: TypeAlias = typing.Union[str, typing.Pattern[str]]
"""A type alias for a matchable path, which can be a string or a compiled regex pattern."""
ExceptionHandler: TypeAlias = typing.Callable[
    [HTTPConnection, Exception], typing.Union[Response, typing.Awaitable[Response]]
]

AwaitableCallable = typing.Callable[..., typing.Awaitable[T]]


class Stringable(typing.Protocol):
    """Protocol for objects that can be converted to a string."""

    def __str__(self) -> str:
        """Return a string representation of the object."""
        ...


class ConnectionIdentifier(typing.Protocol, typing.Generic[HTTPConnectionTcon]):
    """Protocol for connection identifier functions."""

    async def __call__(
        self, connection: HTTPConnectionTcon, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Union[Stringable, typing.Any]:
        """Identify a connection for throttling purposes."""
        ...


class ConnectionThrottledHandler(typing.Protocol, typing.Generic[HTTPConnectionTcon]):
    """Protocol for connection throttled handlers."""

    async def __call__(
        self,
        connection: HTTPConnectionTcon,
        wait_period: WaitPeriod,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        """Handle a throttled connection."""
        ...


class Dependency(typing.Protocol, typing.Generic[P, Rco]):
    """Protocol for dependencies that can be used in FastAPI routes."""

    def __call__(
        self,
        *args: P.args,
        **kwargs: P.kwargs,  # Although FastAPI passes arguments as keyword arguments to dependencies
    ) -> typing.Union[Rco, typing.Awaitable[Rco]]: ...


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
