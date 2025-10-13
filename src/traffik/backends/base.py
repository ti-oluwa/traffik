from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
import functools
import hashlib
import math
import re
import typing

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


def build_key(*args: typing.Any, **kwargs: typing.Any) -> str:
    """Builds a key using the provided parameters."""
    key_parts = [str(arg) for arg in args]
    key_parts.extend(f"{k}={v}" for k, v in kwargs.items())
    if not key_parts:
        return "*"
    key_parts.sort()  # Sort to ensure consistent ordering
    return hashlib.md5(":".join(key_parts).encode()).hexdigest()


class ThrottleBackend(typing.Generic[T, HTTPConnectionT]):
    """
    Base class for throttle backends
    """

    def __init__(
        self,
        connection: T,
        *,
        namespace: str,
        identifier: typing.Optional[ConnectionIdentifier[HTTPConnectionT]] = None,
        handle_throttled: typing.Optional[
            ConnectionThrottledHandler[HTTPConnectionT]
        ] = None,
        persistent: bool = False,
    ) -> None:
        """
        Initialize the throttle backend with a prefix.

        :param connection: The connection to the backend (e.g., Redis).
        :param namespace: The namespace to be used for all throttling keys.
        :param identifier: The connected client identifier generator.
        :param handle_throttled: The handler to call when the client connection is throttled.
        :param persistent: Whether to persist throttling data across application restarts.
        """
        self.connection = connection
        self.namespace = namespace
        self.identifier = identifier or connection_identifier
        self.handle_throttled = handle_throttled or connection_throttled
        self.persistent = persistent

    async def get_key(self, key: str, *args, **kwargs) -> str:
        """
        Get the full key for the given throttling key.

        :param key: The throttling key.
        :return: The full key with namespace.
        """
        if args or kwargs:
            base_key = build_key(key, *args, **kwargs)
            return f"{self.namespace}:{base_key}"
        return f"{self.namespace}:{key}"

    async def initialize(self) -> None:
        """
        Initialize the throttle backend ensuring it is ready for use.
        """
        raise NotImplementedError("`initialize()` must be implemented by the backend.")

    async def get(self, key: str, *args, **kwargs) -> typing.Optional[typing.Any]:
        """
        Get the value for the given key.

        :param key: The throttling key.
        :return: The value associated with the key or None if not found.
        """
        raise NotImplementedError

    async def set(
        self,
        key: str,
        value: typing.Any,
        expire: typing.Optional[int] = None,
        *args,
        **kwargs,
    ) -> None:
        """
        Set the value for the given key.

        :param key: The throttling key.
        :param value: The value to set.
        """
        raise NotImplementedError

    async def delete(self, key: str, *args, **kwargs) -> None:
        """
        Delete the given key.

        :param key: The throttling key to delete.
        """
        raise NotImplementedError

    async def clear(self) -> None:
        """
        Clear all keys in the backend.

        This function clears all keys in the backend.
        """
        raise NotImplementedError

    async def reset(self) -> None:
        """
        Reset the throttle backend.
        """
        await self.clear()

    async def close(self) -> None:
        """Close the backend. Perform cleanup if necessary."""
        pass

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
    ) -> "ThrottleContext[Self]":
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

        return ThrottleContext(
            backend=self,
            persistent=persistent if persistent is not None else self.persistent,
            close_on_exit=close_on_exit,
        )


ThrottleBackendTco = typing.TypeVar(
    "ThrottleBackendTco", bound=ThrottleBackend, covariant=True
)


class ThrottleContext(typing.Generic[ThrottleBackendTco]):
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
