from contextvars import ContextVar
import functools
import math
import re
import typing

from starlette.requests import HTTPConnection
from starlette.exceptions import HTTPException
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from fastapi_throttle._typing import (
    WaitPeriod,
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    T,
    HTTPConnectionT,
)
from fastapi_throttle._utils import get_ip_address
from fastapi_throttle.exceptions import AnonymousConnection


async def connection_identifier(connection: HTTPConnection) -> str:
    client_ip = get_ip_address(connection)
    if not client_ip:
        raise AnonymousConnection("Unable to determine client IP from connection")
    return f"{client_ip.exploded}:{connection.scope['path']}"


async def connection_throttled(
    connection: HTTPConnection, wait_period: WaitPeriod, *args, **kwargs
) -> typing.NoReturn:
    """
    Handler for throttled HTTP connections

    :param connection: The HTTP connection
    :param wait_period: The wait period in milliseconds before the next connection can be made
    :return: None
    """
    expire = math.ceil(wait_period / 1000)
    raise HTTPException(
        status_code=HTTP_429_TOO_MANY_REQUESTS,
        detail="Too Many Requests",
        headers={"Retry-After": str(expire)},
    )


throttle_backend_ctx: ContextVar[typing.Optional["ThrottleBackend"]] = ContextVar(
    "throttle_backend_ctx", default=None
)


class ThrottleBackend(typing.Generic[T, HTTPConnectionT]):
    """
    Base class for throttle backends
    """

    def __init__(
        self,
        connection: T,
        *,
        prefix: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        """
        Initialize the throttle backend with a prefix.

        :param connection: The connection to the backend (e.g., Redis).
        :param prefix: The prefix to be prepended to all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
        """
        self.connection = connection
        self.prefix = prefix
        self.identifier = identifier or connection_identifier
        self.handle_throttled = handle_throttled or connection_throttled
        self.persistent = persistent
        self._context_token = None

    @functools.lru_cache(maxsize=1)
    def get_key_pattern(self) -> re.Pattern:
        """
        Regular expression pattern for throttling keys

        All rate keys are expected to follow this pattern.
        """
        return re.compile(rf"{self.prefix}:*")

    async def check_key_pattern(self, key: str) -> bool:
        """Check if the key matches the throttling key pattern"""
        return re.match(self.get_key_pattern(), key) is not None

    async def initialize(self) -> None:
        pass

    async def get_wait_period(
        self,
        key: str,
        limit: int,
        expires_after: int,
    ) -> int:
        """
        Get the wait period for the given key.

        :param key: The throttling key.
        :param limit: The maximum number of requests allowed within the time period.
        :param expire_time: The time period in milliseconds for which the key is valid.
        :return: The wait period in milliseconds.
        """
        raise NotImplementedError

    async def reset(self) -> None:
        """
        Reset the throttling keys.

        This function clears all throttling keys in the backend.

        :param keys: The throttling keys to reset.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Close the backend connection."""
        raise NotImplementedError

    async def __aenter__(self) -> "ThrottleBackend[T, HTTPConnectionT]":
        """Context manager entry point."""
        await self.initialize()
        # Set the throttle backend in the context variable
        self._context_token = throttle_backend_ctx.set(self)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Context manager exit point."""
        if self._context_token is not None:
            throttle_backend_ctx.reset(self._context_token)
            self._context_token = None

        if not self.persistent:
            # Reset the backend if not persistent
            await self.reset()
        await self.close()


def get_throttle_backend() -> typing.Optional[ThrottleBackend]:
    """
    Get the current throttle backend from the context variable.

    :return: The current throttle backend or None if not set.
    """
    return throttle_backend_ctx.get(None)


__all__ = [
    "ThrottleBackend",
    "connection_identifier",
    "connection_throttled",
    "get_throttle_backend",
]
