from dataclasses import dataclass, field
import typing

from annotated_types import Ge
from starlette.requests import HTTPConnection
from starlette.responses import Response
from typing_extensions import Annotated
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


@dataclass(frozen=True)
class Rate:
    """Rate limit definition"""

    limit: Annotated[int, Ge(0)] = 0
    """Maximum number of allowed requests in the time window. 0 means no limit."""
    milliseconds: Annotated[int, Ge(0)] = 0
    """Time period in milliseconds"""
    seconds: Annotated[int, Ge(0)] = 0
    """Time period in seconds"""
    minutes: Annotated[int, Ge(0)] = 0
    """Time period in minutes"""
    hours: Annotated[int, Ge(0)] = 0
    """Time period in hours"""
    _expire: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("Limit must be non-negative")
        expire = (
            self.milliseconds
            + 1000 * self.seconds
            + 60000 * self.minutes
            + 3600000 * self.hours
        )
        if expire < 0:
            raise ValueError("Time period must be non-negative")
        if self.limit == 0 and expire != 0:
            raise ValueError("Expire must be 0 when limit is 0")
        if self.limit != 0 and expire == 0:
            raise ValueError("Expire must be greater than 0 when limit is set")

        object.__setattr__(self, "_expire", expire)

    @property
    def expire(self) -> float:
        """Total time period in milliseconds, per limit"""
        return self._expire

    @property
    def unlimited(self) -> bool:
        """Whether the rate limit is unlimited"""
        return self.limit == 0 and self.expire == 0

    @property
    def rps(self) -> float:
        """Requests per second"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 1000)

    @property
    def rpm(self) -> float:
        """Requests per minute"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 60000)

    @property
    def rph(self) -> float:
        """Requests per hour"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 3600000)

    @property
    def rpd(self) -> float:
        """Requests per day"""
        if self.limit == 0 or self.expire == 0:
            return float("inf")
        return self.limit / (self.expire / 86400000)

    @classmethod
    def from_string(cls, rate: str) -> "Rate":
        """
        Create a ``z`` object from a string representation.

        The string should be in the format "<number>/<period>",
        where <period> can be "s" (seconds), "m" (minutes), "h" (hours), or "d" (days).
        For example, "5/m" means 5 requests per minute.

        :param rate: The string representation of the rate limit.
        :return: A `Rate` object.
        """
        try:
            limit_str, period_str = rate.split("/")
            limit = int(limit_str)
            period_str = period_str.strip().lower()
            if period_str in ("sec", "s", "second", "seconds"):
                return cls(limit=limit, seconds=1)
            elif period_str in ("min", "m", "minute", "minutes"):
                return cls(limit=limit, minutes=1)
            elif period_str in ("h", "hour", "hours"):
                return cls(limit=limit, hours=1)
            elif period_str in ("d", "day", "days"):
                return cls(limit=limit, hours=24)
            else:
                raise ValueError(f"Invalid period '{period_str}' in rate string")
        except Exception as exc:
            raise ValueError(f"Invalid rate string '{rate}': {exc}") from exc
