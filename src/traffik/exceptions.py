"""`traffik` exceptions and exception handling utilities."""

import http
import typing

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS
from starlette.types import ExceptionHandler as StarletteExceptionHandler

from traffik.types import ExceptionHandler as TraffikExceptionHandler


class TraffikException(Exception):
    """Base exception for all Traffik-related errors."""

    pass


class ConfigurationError(TraffikException):
    """Exception raised when the throttle configuration is invalid."""

    pass


class BackendError(TraffikException):
    """Exception raised for backend related errors."""

    def __init__(
        self,
        *args: typing.Any,
        cause: typing.Optional[BaseException] = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cause = cause

    @property
    def cause(self) -> typing.Optional[BaseException]:
        """
        The underlying cause of the backend error, if any.

        Basically the exception that triggered this exception.
        """
        return self._cause or self.__cause__


class BackendConnectionError(BackendError):
    """Backend connection error"""

    pass


class LockTimeoutError(BackendError, TimeoutError):
    """Exception raised when a lock timeout occurs"""

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
            detail = http.HTTPStatus(status_code).phrase
            if wait_period is not None:
                detail += f" Retry after {wait_period} second(s)."

        if wait_period is not None:
            headers = dict(headers or {})
            headers["Retry-After"] = str(wait_period)
        super().__init__(status_code, detail, headers)
        self.wait_period = wait_period


# Borrowed from Starlette's exception handling utilities
def _lookup_exception_handler(
    exc_handlers: typing.Mapping[typing.Type[typing.Any], StarletteExceptionHandler],
    exc: Exception,
) -> typing.Optional[StarletteExceptionHandler]:
    for cls in type(exc).__mro__:
        if cls in exc_handlers:
            return exc_handlers[cls]
    return None


def build_exception_handler_getter(
    app: Starlette,
) -> typing.Callable[[Exception], typing.Optional[TraffikExceptionHandler]]:
    """
    Build an exception handler getter for the given Starlette app.
    """
    # Here we use a neat trick to retrieve the exception handlers from the app
    # by creating an instance of `ExceptionMiddleware` with the app and its handlers,
    # and trying to resolve the handler from both the status handlers and the exception handlers.
    exception_middleware = ExceptionMiddleware(
        app,
        handlers=app.exception_handlers,  # type: ignore[call-arg]
        debug=app.debug,
    )

    def handler_getter(exc: Exception) -> typing.Optional[TraffikExceptionHandler]:
        """
        Get the exception handler for the given exception.

        :param exc: The exception for which to retrieve the handler.
        :return: The exception handler function.
        """
        nonlocal exception_middleware

        handler: typing.Optional[StarletteExceptionHandler] = None
        if isinstance(exc, HTTPException):
            handler = exception_middleware._status_handlers.get(exc.status_code)

        if handler is None:
            handler = _lookup_exception_handler(
                exception_middleware._exception_handlers, exc
            )
        return typing.cast(typing.Optional[TraffikExceptionHandler], handler)

    return handler_getter
