import typing
import functools
import hashlib
from typing_extensions import Annotated
from annotated_types import Ge
from starlette.websockets import WebSocket
from starlette.requests import Request
from starlette.responses import Response

from fastapi_throttle._typing import (
    T,
    HTTPConnectionT,
    CoroutineFunction,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
)
from fastapi_throttle.backends.base import ThrottleBackend, get_throttle_backend
from fastapi_throttle.exceptions import NoLimit, ConfigurationError


__all__ = [
    "BaseThrottle",
    "HTTPThrottle",
    "WebSocketThrottle",
]


class ThrottleMeta(type):
    def __new__(cls, name, bases, attrs):
        new_cls = super().__new__(cls, name, bases, attrs)
        new_cls.__call__ = cls._capture_no_limit(new_cls.__call__)
        new_cls.get_key = cls._wrap_get_key(new_cls.get_key)  # type: ignore
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

    @staticmethod
    def _wrap_get_key(get_key: CoroutineFunction) -> CoroutineFunction:
        """Wraps the `get_key` method to ensure the pattern of the key returned is valid"""

        @functools.wraps(get_key)
        async def wrapper(self: "BaseThrottle", *args, **kwargs) -> str:
            key = await get_key(self, *args, **kwargs)
            if not isinstance(key, str):
                raise TypeError("Generated throttling key should be a string")

            backend = self.backend
            if not await backend.check_key_pattern(key):
                raise ValueError(
                    "Invalid throttling key pattern. "
                    f"Key must be in the format: {backend.get_key_pattern()}"
                )
            return key

        return wrapper


class BaseThrottle(typing.Generic[HTTPConnectionT], metaclass=ThrottleMeta):
    """
    Base class for throttles
    """

    def __init__(
        self,
        limit: Annotated[int, Ge(0)] = 1,
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

        backend = backend or get_throttle_backend()
        if not isinstance(backend, ThrottleBackend):
            raise ConfigurationError("No throttle backend provided or detected.")

        self.backend = backend
        self.identifier = identifier or backend.identifier
        self.handle_throttled = handle_throttled or backend.handle_throttled

    async def __call__(
        self, connection: HTTPConnectionT, *args, **kwargs
    ) -> typing.Any:
        backend = self.backend
        key = await self.get_key(connection, *args, **kwargs)
        wait_period = await backend.get_wait_period(
            key, limit=self.limit, expires_after=self.expires_after
        )

        if wait_period != 0 and self.handle_throttled:
            await self.handle_throttled(connection, wait_period, *args, **kwargs)
        return None

    async def get_key(self, connection: HTTPConnectionT, *args, **kwargs) -> str:
        """
        Returns the unique throttling key for the client.

        Key returned must match the pattern returned by `backend.get_key_pattern`,
        otherwise a ValueError is raised on key generation.
        """
        raise NotImplementedError


class HTTPThrottle(BaseThrottle[Request]):
    """HTTP connection throttle"""

    async def get_key(self, request: Request, response: Response) -> str:
        route_index = 0
        dependency_index = 0
        for i, route in enumerate(request.app.routes):
            if route.path == request.scope["path"] and request.method in route.methods:
                route_index = i
                for j, dependency in enumerate(route.dependencies):
                    if self is dependency.dependency:
                        dependency_index = j
                        break

        rate_key = await self.identifier(request)
        suffix = f"{rate_key}:{route_index}:{dependency_index}:{id(self)}"
        hashed_suffix = hashlib.md5(suffix.encode()).hexdigest()
        key = f"{self.backend.prefix}:http:{hashed_suffix}"
        # Added id(self) to ensure unique key for each throttle instance
        # in the advent that the dependency index is not unique. Especially when
        # used with the `throttle` decorator.
        return key

    async def __call__(self, connection: Request, response: Response):
        return await super().__call__(connection, response=response)


class WebSocketThrottle(BaseThrottle[WebSocket]):
    """WebSocket connection throttle"""

    async def get_key(
        self, connection: WebSocket, context_key: typing.Optional[str] = None
    ) -> str:
        rate_key = await self.identifier(connection)
        suffix = f"{rate_key}:{connection.url.path}:{id(self)}:{context_key or ''}"
        hashed_suffix = hashlib.md5(suffix.encode()).hexdigest()
        key = f"{self.backend.prefix}:ws:{hashed_suffix}"
        # Added id(self) to ensure unique key for each throttle instance
        # in the advent that the context key is not unique. Especially when
        # used with the `throttle` decorator.
        return key

    async def __call__(
        self, connection: WebSocket, context_key: typing.Optional[str] = None
    ) -> typing.Any:
        return await super().__call__(connection, context_key=context_key)
