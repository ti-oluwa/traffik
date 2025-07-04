import re
import typing

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from typing_extensions import TypeAlias

from traffik.backends.base import ThrottleBackend, get_throttle_backend
from traffik.throttles import BaseThrottle
from traffik.types import HTTPConnectionT, Matchable

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
        HTTPThrottle(limit=10, seconds=60),
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
        methods: typing.Optional[typing.Set[str]] = None,
        hook: typing.Optional[ThrottleHook[HTTPConnectionT]] = None,
    ) -> None:
        """
        Initialize the middleware throttle.

        :param throttle: The throttle to apply to the connection.
        :param path: A matchable path (string or regex) to apply the throttle to.
            If string, it's compiled as a regex pattern.

            Examples:

                - "/api/" matches paths starting with "/api/"
                - r"/api/\d+" matches "/api/" followed by digits
                - None applies to all paths.

        :param methods: A set of HTTP methods (e.g., 'GET', 'POST') to apply the throttle to.
            If None, the throttle applies to all methods.
        :param hook: An optional callable that takes an HTTP connection and returns a boolean.
            If provided, the throttle will only apply if this hook returns True for the connection.
            This is useful for more complex conditions that cannot be expressed with just path and methods.
            It is run after checking the path and methods, so it should be used for more expensive checks.
        """
        self.throttle = throttle

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


class ThrottleMiddleware:
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
                HTTPThrottle(limit=10, seconds=60),
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
        middleware_throttles: typing.Sequence[MiddlewareThrottle[HTTPConnection]],
        backend: typing.Optional[ThrottleBackend[typing.Any, HTTPConnection]] = None,
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

        connection = HTTPConnection(scope, receive)
        if self.backend is None:
            backend = get_throttle_backend(connection)
            if backend is None:
                raise RuntimeError("No throttle backend configured.")
            self.backend = backend

        async with self.backend(close_on_exit=False):
            for throttle in self.middleware_throttles:
                connection = await throttle(connection)

        await self.app(scope, receive, send)
