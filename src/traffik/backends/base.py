import functools
import math
import re
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp
from typing_extensions import Self

from traffik._utils import get_ip_address
from traffik.exceptions import AnonymousConnection, ConnectionThrottled
from traffik.types import (
    ConnectionIdentifier,
    ConnectionThrottledHandler,
    HTTPConnectionT,
    T,
    WaitPeriod,
)


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
    wait_seconds = math.ceil(wait_period / 1000)
    raise ConnectionThrottled(wait_period=wait_seconds)


throttle_backend_ctx: ContextVar[typing.Optional["ThrottleBackend"]] = ContextVar(
    "throttle_backend_ctx", default=None
)

BACKEND_STATE_KEY = "__traffik_throttle_backend"


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

    @functools.cached_property
    def key_pattern(self) -> re.Pattern:
        """
        Regular expression pattern for throttling keys

        All rate keys are expected to follow this pattern.
        """
        return self.get_key_pattern()

    def get_key_pattern(self) -> re.Pattern:
        """
        Regular expression pattern for throttling keys

        All rate keys are expected to follow this pattern.
        """
        return re.compile(rf"{self.prefix}:*")

    async def check_key_pattern(self, key: str) -> bool:
        """Check if the key matches the throttling key pattern"""
        return re.match(self.key_pattern, key) is not None

    async def initialize(self) -> None:
        """
        Initialize the throttle backend ensuring it is ready for use.
        """
        raise NotImplementedError(
            "The initialize method must be implemented by the backend."
        )

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

    @asynccontextmanager
    async def lifespan(self, app: ASGIApp) -> typing.AsyncIterator[None]:
        """
        ASGI lifespan context manager for the throttle backend.
        """
        async with self(app):
            yield

    def __call__(
        self,
        app: typing.Optional[ASGIApp] = None,
        persistent: typing.Optional[bool] = None,
        close_on_exit: bool = True,
    ) -> "_ThrottleContext[Self]":
        """
        Create a throttle context for the backend.

        :param app: The ASGI application to assign the backend to.
        :param persistent: Whether to keep the backend state across application restarts.
            This overrides the backend's persistent setting if set.
        :param close_on_exit: Whether to close the backend when exiting the context.
        :return: A context manager for the throttle backend.
        """
        if app is not None:
            # Ensure app.state exists
            app.state = getattr(app, "state", {})  # type: ignore
            setattr(app.state, BACKEND_STATE_KEY, self)  # type: ignore

        return _ThrottleContext(
            backend=self,
            persistent=persistent if persistent is not None else self.persistent,
            close_on_exit=close_on_exit,
        )


ThrottleBackendTco = typing.TypeVar(
    "ThrottleBackendTco", bound=ThrottleBackend, covariant=True
)


class _ThrottleContext(typing.Generic[ThrottleBackendTco]):
    """
    Context manager for throttle backends.
    """

    def __init__(
        self,
        backend: ThrottleBackendTco,
        persistent: bool = False,
        close_on_exit: bool = True,
    ) -> None:
        self.backend = backend
        self.persistent = persistent
        self.close_on_exit = close_on_exit
        self._context_token: typing.Optional[
            Token[typing.Optional[ThrottleBackend]]
        ] = None

    async def __aenter__(self) -> ThrottleBackendTco:
        backend = self.backend
        await backend.initialize()
        # Set the throttle backend in the context variable
        self._context_token = throttle_backend_ctx.set(backend)
        return backend

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self._context_token is not None:
            throttle_backend_ctx.reset(self._context_token)
            self._context_token = None

        backend = self.backend
        if not self.persistent:
            # Reset the backend if not persistent
            await backend.reset()

        if self.close_on_exit:
            await backend.close()


def get_throttle_backend(
    connection: typing.Optional[HTTPConnectionT] = None,
) -> typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]]:
    """
    Get the current context's throttle backend or provided connection's throttle backend.

    :param connection: The HTTP connection to check for a throttle backend.
    :return: The current throttle backend or None if not set.
    """
    # Try to get from contextvar, then check `connection.app.state`
    backend = throttle_backend_ctx.get(None)
    if backend is None and connection is not None:
        backend = getattr(connection.app.state, BACKEND_STATE_KEY, None)
    return typing.cast(
        typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]], backend
    )


__all__ = [
    "ThrottleBackend",
    "connection_identifier",
    "connection_throttled",
    "get_throttle_backend",
]
