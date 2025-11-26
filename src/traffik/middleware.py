"""Throttle and ASGI middleware for throttling HTTP connections."""

import re
import typing

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.exceptions import ConfigurationError, build_exception_handler_getter
from traffik.throttles import BaseThrottle
from traffik.types import HTTPConnectionT, Matchable
from traffik.utils import is_async_callable

ThrottleHook: TypeAlias = typing.Callable[[HTTPConnectionT], typing.Awaitable[bool]]
"""
A type alias for a callable that takes an HTTP connection and returns a boolean.

This is used as a hook to determine if the throttle should apply.
"""


class MiddlewareThrottle(typing.Generic[HTTPConnectionT]):
    """
    Middleware throttle.

    This throttle applies to HTTP connections based on the specified path, methods, and an optional hook.
    These criteria are checked before applying the throttle to the connection, from least expensive to most expensive.
    That is, it first checks the HTTP method, then the path, and finally the hook if provided.

    If the connection does not match the criteria, it is returned unchanged.

    Usage:
    ```python
    from starletter.applications import Starlette

    from traffik.middleware import MiddlewareThrottle
    from traffik.throttles import HTTPThrottle
    from traffik.backends.inmemory import InMemoryBackend
    from traffik.middleware import ThrottleMiddleware

    # Use a custom hook to use throttle for only premium users.
    async def premium_user_hook(connection: HTTPConnection) -> bool:
        # Check if the user is a premium user
        return connection.headers.get("X-User-Tier") == "premium"

    middleware_throttle = MiddlewareThrottle(
        HTTPThrottle(uid="...", rate="10/min"),
        path="/api/",
        methods={"GET", "POST"},
        hook=premium_user_hook,
    )

    # Use the middleware throttle in your application
    throttle_backend = InMemoryBackend()
    app = Starlette(lifespan=throttle_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[middleware_throttle],
        backend=throttle_backend,
    )
    ```
    """

    def __init__(
        self,
        throttle: BaseThrottle[HTTPConnectionT],
        path: typing.Optional[Matchable] = None,
        methods: typing.Optional[typing.Iterable[str]] = None,
        hook: typing.Optional[ThrottleHook[HTTPConnectionT]] = None,
    ) -> None:
        """
        Initialize the middleware throttle.

        :param throttle: The throttle to apply to the connection.
        :param path: A matchable path (string or regex) to apply the throttle to.
            If string, it's compiled as a regex pattern.

            Examples:

                - "/api/" matches paths starting with "/api/"
                - r"/api/\\d+" matches "/api/" followed by digits
                - None applies to all paths.

        :param methods: A set of HTTP methods (e.g., 'GET', 'POST') to apply the throttle to.
            If None, the throttle applies to all methods.
        :param hook: An optional callable that takes an HTTP connection and returns a boolean.
            If provided, the throttle will only apply if this hook returns True for the connection.
            This is useful for more complex conditions that cannot be expressed with just path and methods.
            It is run after checking the path and methods, so it should be used for more expensive checks.
        """
        self.throttle = throttle

        self.path: typing.Optional[re.Pattern[str]]
        if isinstance(path, str):
            self.path = re.compile(path)
        else:
            self.path = path

        if methods is None:
            self.methods = None
        else:
            self.methods = frozenset(
                method.lower() for method in methods if isinstance(method, str)
            )
        self.hook = hook

    async def __call__(self, connection: HTTPConnectionT) -> HTTPConnectionT:
        """
        Checks if the throttle applies to the connection and applies it if so.

        :param connection: The HTTP connection to check.
        :return: The connection, possibly modified by the throttle. If throttling criteria
            are not met, returns the original connection unchanged. If throttled, may return
            a modified connection or raise a throttling exception.
        :raises: `HTTPException` if the connection exceeds rate limits.
        """
        if (
            self.methods is not None
            and connection.scope["method"].lower() not in self.methods
        ):
            return connection
        if self.path is not None and not self.path.match(connection.scope["path"]):
            return connection
        if self.hook is not None and not await self.hook(connection):
            return connection

        return await self.throttle(connection)


class ThrottleMiddleware(typing.Generic[HTTPConnectionT]):
    """
    ASGI middleware that applies throttling to HTTP connections.

    This middleware processes incoming HTTP connections and applies throttles based on
    the provided `MiddlewareThrottle` instances. It integrates with throttle backends
    to manage throttling state across connections.

    Usage:
    ```python
    from starletter.applications import Starlette

    from traffik.backends.inmemory import InMemoryBackend
    from traffik.middleware import ThrottleMiddleware
    from traffik.throttles import HTTPThrottle

    throttle_backend = InMemoryBackend()

    app = Starlette(lifespan=throttle_backend.lifespan)
    app.add_middleware(
        ThrottleMiddleware,
        middleware_throttles=[
            MiddlewareThrottle(
                HTTPThrottle(rate="5/min", uid="..."),
                path="/api/",
                methods={"GET", "POST"},
            )
        ],
        backend=throttle_backend, # Optional, can be omitted to use the context(lifespan) backend
    )
    ... # Other routes and/or middleware
    ```

    """

    def __init__(
        self,
        app: ASGIApp,
        middleware_throttles: typing.Sequence[MiddlewareThrottle[HTTPConnectionT]],
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnectionT]] = None,
    ) -> None:
        """
        Initialize the middleware with the application and throttles.

        :param app: The ASGI application to wrap.
        :param middleware_throttles: A sequence of `MiddlewareThrottle` instances to apply.
        :param backend: An optional throttle backend to use.
        """
        self.app = app
        self.middleware_throttles = middleware_throttles
        self.backend = backend or get_throttle_backend()
        self.get_exception_handler = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The ASGI application callable that applies throttles to the connection.

        :param scope: The ASGI scope.
        :param receive: The receive function for incoming messages.
        :param send: The send function for outgoing messages.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        connection = typing.cast(HTTPConnectionT, HTTPConnection(scope, receive))
        if self.backend is None:
            backend = get_throttle_backend(connection.app)
            if backend is None:
                raise ConfigurationError("No throttle backend configured.")
            self.backend = backend

        # The backend context must be not closed on context exit.
        # It must also be persistent to ensure throttles can maintain
        # state across multiple connections.
        async with self.backend(close_on_exit=False, persistent=True):
            for throttle in self.middleware_throttles:
                try:
                    connection = await throttle(connection)
                except Exception as exc:
                    # This approach allows custom throttles to raise custom exceptions
                    # that will be handled if they register an exception handler with
                    # the application. If not, the exception will propagate and the
                    # `ServerErrorMiddleware` will properly handle it.
                    exc_handler = self.get_exception_handler
                    if exc_handler is None:
                        exc_handler = build_exception_handler_getter(connection.app)
                        # Cache the exception handler getter for future use
                        self.get_exception_handler = exc_handler  # type: ignore[assignment]

                    handler = exc_handler(exc)
                    if handler is not None:
                        if is_async_callable(handler):
                            response = await handler(connection, exc)
                        else:
                            response = await run_in_threadpool(handler, connection, exc)

                        if response is not None:
                            await response(scope, receive, send)  # type: ignore[call-arg]
                            return

                    raise exc

        # Ensure that the next middleware or application call is not nested
        # within this middleware's backend context. Else, it would cause
        # the next call to use the same backend context as this middleware,
        # even when it supposed to use the backend context set on lifespan.
        await self.app(scope, receive, send)
