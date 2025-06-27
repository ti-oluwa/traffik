import functools
import hashlib
import typing

from annotated_types import Ge
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket
from typing_extensions import Annotated

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import ConfigurationError, NoLimit
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    CoroutineFunction,
    HTTPConnectionT,
    T,
)

__all__ = [
    "BaseThrottle",
    "HTTPThrottle",
    "WebSocketThrottle",
]


class ThrottleMeta(type):
    def __new__(cls, name, bases, attrs):
        new_cls = super().__new__(cls, name, bases, attrs)
        new_cls.__call__ = cls._capture_no_limit(new_cls.__call__)
        return new_cls

    @staticmethod
    def _capture_no_limit(coroutine_func: CoroutineFunction) -> CoroutineFunction:
        """
        Wraps the coroutine function such that NoLimit exceptions are caught
        and ignored, returning None instead.

        :param func: The coroutine function to wrap
        """

        @functools.wraps(coroutine_func)
        async def wrapper(*args, **kwargs):
            try:
                return await coroutine_func(*args, **kwargs)
            except NoLimit:
                return

        return wrapper


class BaseThrottle(typing.Generic[HTTPConnectionT], metaclass=ThrottleMeta):
    """
    Base class for throttles
    """

    def __init__(
        self,
        limit: Annotated[int, Ge(0)] = 0,
        milliseconds: Annotated[int, Ge(0)] = 0,
        seconds: Annotated[int, Ge(0)] = 0,
        minutes: Annotated[int, Ge(0)] = 0,
        hours: Annotated[int, Ge(0)] = 0,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        backend: typing.Optional[ThrottleBackend[T, HTTPConnectionT]] = None,
    ) -> None:
        """
        Initialize the throttle

        :param limit: Maximum number of times the route can be accessed within specified time period
        :param milliseconds: Time period in milliseconds
        :param seconds: Time period in seconds
        :param minutes: Time period in minutes
        :param hours: Time period in hours
        :param identifier: Connected client identifier generator.
            If not provided, the throttle backend's identifier will be used.
        :param handle_throttled: Handler to call when the client connection is throttled.
            If provided, it will override the default connection throttled handler
            defined for the throttle backend.
        :param backend: The throttle backend to use for storing throttling data.
            If not provided, the default backend will be used.
        """
        self.limit = limit
        self.expires_after = (
            milliseconds + 1000 * seconds + 60000 * minutes + 3600000 * hours
        )
        if self.expires_after < 0:
            raise ValueError("Time period must be non-negative")
        if self.limit < 0:
            raise ValueError("Limit must be non-negative")

        self.backend = backend or get_throttle_backend()
        self.identifier = identifier or (
            self.backend.identifier if self.backend is not None else None
        )
        self.handle_throttled = handle_throttled or (
            self.backend.handle_throttled if self.backend is not None else None
        )

    async def __call__(
        self, connection: HTTPConnectionT, *args: typing.Any, **kwargs: typing.Any
    ) -> None:
        """
        Throttle the connection based on the limit and time period.

        :param connection: The HTTP or WebSocket connection to throttle.
        :param args: Additional positional arguments to pass to the throttled handler.
        :param kwargs: Additional keyword arguments to pass to the throttled handler.
        :raises NoLimit: If the limit is set to 0, this method will return None.
        """
        if self.limit == 0 or self.expires_after == 0:
            return  # No throttling applied if limit is 0

        backend = self.backend = self.backend or get_throttle_backend(connection)
        if backend is None:
            raise ConfigurationError(
                "No throttle backend configured. "
                "Provide a backend to the throttle or set a default backend."
            )

        identifier = self.identifier = self.identifier or backend.identifier
        key = await self.get_key(identifier, connection, *args, **kwargs)
        backend_key = f"{backend.prefix}:{key}"
        if not await backend.check_key_pattern(backend_key):
            raise ValueError(
                "Invalid throttling key pattern. "
                f"Key must be in the format: {backend.get_key_pattern()}"
            )

        wait_period = await backend.get_wait_period(
            backend_key, self.limit, self.expires_after
        )

        if wait_period != 0:
            handle_throttled = self.handle_throttled or backend.handle_throttled
            await handle_throttled(connection, wait_period, *args, **kwargs)
        return None

    async def get_key(
        self,
        identifier: ConnectionIdentifier[HTTPConnectionT],
        connection: HTTPConnectionT,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> str:
        """
        Returns the unique throttling key for the client.

        Key returned must match the pattern returned by `backend.get_key_pattern`,
        otherwise a ValueError is raised on key generation.
        """
        raise NotImplementedError


class HTTPThrottle(BaseThrottle[Request]):
    """HTTP connection throttle"""

    async def get_key(
        self,
        identifier: ConnectionIdentifier[Request],
        request: Request,
        response: Response,
    ) -> str:
        route_index = 0
        dependency_index = 0
        for i, route in enumerate(request.app.routes):
            if route.path == request.scope["path"] and request.method in route.methods:
                route_index = i
                for j, dependency in enumerate(route.dependencies):
                    if self is dependency.dependency:
                        dependency_index = j
                        break

        rate_key = await identifier(request)
        suffix = f"{rate_key}:{route_index}:{dependency_index}:{id(self)}"
        hashed_suffix = hashlib.md5(suffix.encode()).hexdigest()  # nosec
        throttle_key = f"http:{hashed_suffix}"
        # Added id(self) to ensure unique key for each throttle instance
        # in the advent that the dependency index is not unique. Especially when
        # used with the `throttle` decorator.
        return throttle_key

    async def __call__(self, connection: Request, response: Response) -> None:
        """
        Calls the throttle for an HTTP connection.

        :param connection: The HTTP connection to throttle.
        :param response: The HTTP response to be returned if throttled.
        """
        return await super().__call__(connection, response=response)


class WebSocketThrottle(BaseThrottle[WebSocket]):
    """WebSocket connection throttle"""

    async def get_key(
        self,
        identifier: ConnectionIdentifier[WebSocket],
        connection: WebSocket,
        context_key: typing.Optional[str] = None,
    ) -> str:
        rate_key = await identifier(connection)
        suffix = f"{rate_key}:{connection.url.path}:{id(self)}:{context_key or ''}"
        hashed_suffix = hashlib.md5(suffix.encode()).hexdigest()  # nosec
        throttle_key = f"ws:{hashed_suffix}"
        # Added id(self) to ensure unique key for each throttle instance
        # in the advent that the context key is not unique. Especially when
        # used with the `throttle` decorator.
        return throttle_key

    async def __call__(
        self, connection: WebSocket, context_key: typing.Optional[str] = None
    ) -> None:
        """
        Calls the throttle for a WebSocket connection.

        :param connection: The WebSocket connection to throttle.
        :param context_key: Optional context key to differentiate throttling
            for different contexts within the same WebSocket connection.
        """
        return await super().__call__(connection, context_key=context_key)
