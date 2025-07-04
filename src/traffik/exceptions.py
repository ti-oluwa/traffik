import typing

from starlette.exceptions import HTTPException
from starlette.status import HTTP_429_TOO_MANY_REQUESTS


class TraffikException(Exception):
    """Base exception for all Traffik-related errors."""

    pass


class ConfigurationError(TraffikException):
    """Exception raised when the throttle configuration is invalid."""

    pass


class AnonymousConnection(TraffikException):
    """
    Exception raised when the connection identifier cannot be determined.

    This exception is raised when the throttle backend cannot generate a unique identifier
    for the connection, which is necessary for throttling to work correctly.
    """

    pass


class ConnectionThrottled(HTTPException, TraffikException):
    """
    `HTTPException` raised when a connection is throttled.

    This exception is used to indicate that a connection has exceeded its allowed rate limit
    and must wait before making further requests.

    It includes a `wait_period` attribute that specifies how long in seconds, the client should wait
    before retrying the request.
    """

    def __init__(
        self,
        wait_period: typing.Optional[int] = None,
        status_code: int = HTTP_429_TOO_MANY_REQUESTS,
        detail: typing.Optional[str] = None,
        headers: typing.Optional[typing.Mapping[str, str]] = None,
    ) -> None:
        if detail is None:
            if wait_period is not None:
                detail = f"Too Many Requests. Retry after {wait_period} seconds."
            else:
                detail = "Too Many Requests"

        if wait_period is not None:
            headers = dict(headers or {})
            headers["Retry-After"] = str(wait_period)
        super().__init__(status_code, detail, headers)
        self.wait_period = wait_period
